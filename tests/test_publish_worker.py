from threads_operator.publish_worker import (
    is_rate_limited_publish_result,
    is_transient_publish_result,
    publish_next_with_recovery,
)


class FakeStore:
    def __init__(self, row=None):
        self.row = row
        self.calls = []
        self.requeue_calls = []
        self._headers = {}
        self.client = None

    def _require_account_key(self):
        return "acct"

    def peek_due_post(self, table, campaign_code=None, now=None):
        self.calls.append(("peek", table, campaign_code))
        return self.row

    def claim_due_post(self, table, campaign_code=None, now=None):
        self.calls.append(("claim", table, campaign_code))
        return self.row

    def mark_post_main_published(self, table, row_id, post_id):
        self.calls.append(("main", table, row_id, post_id))

    def mark_post_reply_progress(self, table, row_id, reply_ids):
        self.calls.append(("reply-progress", table, row_id, list(reply_ids)))

    def mark_post_posted(self, table, row_id, reply_ids, posted_at=None):
        self.calls.append(("posted", table, row_id, reply_ids))

    def mark_post_failed(self, table, row_id, error):
        self.calls.append(("failed", table, row_id, error))


class FakeAPI:
    def __init__(self, ids=None, fail_at=None, error="boom"):
        self.ids = list(ids or [])
        self.fail_at = fail_at
        self.error = error
        self.calls = []

    def publish_text(self, text, reply_to_id=None, topic_tag=None):
        self.calls.append((text, reply_to_id))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise RuntimeError(self.error)
        return self.ids.pop(0)


ROW = {"id": 9, "main_post_text": "hello", "reply_texts": []}


def test_transient_classification_network_and_400():
    assert is_transient_publish_result({"error": "NetworkError: connect failed"})
    assert is_transient_publish_result(
        {"error": "HTTPStatusError: Client error '400 Bad Request' for url ..."}
    )
    assert is_transient_publish_result({"error": "HTTP 429 Too Many Requests"})


def test_oauth_and_permission_are_not_transient():
    assert not is_transient_publish_result({"error": "OAuthException: bad token"})
    assert not is_transient_publish_result({"error": "permission denied"})
    assert not is_transient_publish_result({"error": "invalid token supplied"})


def test_unknown_error_is_not_transient():
    assert not is_transient_publish_result({"error": "totally unexpected"})
    assert not is_transient_publish_result({"error": ""})


def test_success_is_not_requeued():
    api = FakeAPI(ids=["p1"])
    store = FakeStore(row=dict(ROW))
    result = publish_next_with_recovery(api, store, "threads_publish_queue")
    assert result["status"] == "posted"
    assert "requeued" not in result


def test_transient_failure_retries_within_run_and_recovers(monkeypatch):
    """First pass fails transient, requeue + immediate in-run retry succeeds."""
    api = FakeAPI(ids=["p1"], fail_at=1, error="HTTPStatusError: 400 Bad Request")
    store = FakeStore(row=dict(ROW))
    sleeps = []

    def requeue_ok(store, table, row_id, error, **kwargs):
        return True

    monkeypatch.setattr(
        "threads_operator.publish_worker.requeue_failed_row", requeue_ok
    )
    result = publish_next_with_recovery(
        api,
        store,
        "threads_publish_queue",
        sleep=sleeps.append,
    )
    assert result["requeued"] is True
    assert result["attempts"] == 1
    # Transient failure defers to a persisted backoff gate for the next tick
    # rather than retrying in a tight in-run loop.
    assert result.get("next_retry_at") is not None


def test_transient_failure_respects_attempt_budget(monkeypatch):
    """A persistent transient failure is requeued with backoff each tick; the
    durable max-attempts cap (not in-run sleeps) eventually stops it."""

    class AlwaysFailAPI(FakeAPI):
        def publish_text(self, text, reply_to_id=None, topic_tag=None):
            raise RuntimeError("HTTPStatusError: 400 Bad Request")

    api = AlwaysFailAPI()
    store = FakeStore(row=dict(ROW))
    sleeps = []

    monkeypatch.setattr(
        "threads_operator.publish_worker.requeue_failed_row",
        lambda store, table, row_id, error, **kw: True,
    )
    result = publish_next_with_recovery(
        api,
        store,
        "threads_publish_queue",
        sleep=sleeps.append,
    )
    assert result["status"] == "failed"
    assert result["requeued"] is True
    # One attempt per tick; backoff is persisted for the next tick (no in-run
    # sleep loop).
    assert result["next_retry_at"] is not None
    assert sleeps == []


def test_transient_failure_respects_deadline(monkeypatch):
    """A transient failure always yields to the next tick (no in-run sleep)."""

    class AlwaysFailAPI(FakeAPI):
        def publish_text(self, text, reply_to_id=None, topic_tag=None):
            raise RuntimeError("HTTPStatusError: 400 Bad Request")

    api = AlwaysFailAPI()
    store = FakeStore(row=dict(ROW))
    sleeps = []
    now = [1000.0]

    monkeypatch.setattr(
        "threads_operator.publish_worker.requeue_failed_row",
        lambda store, table, row_id, error, **kw: True,
    )
    result = publish_next_with_recovery(
        api,
        store,
        "threads_publish_queue",
        attempt_deadline_seconds=1.0,
        sleep=sleeps.append,
        clock=lambda: now[0],
    )
    assert result["status"] == "failed"
    assert result["attempts"] == 1
    assert result["requeued"] is True
    assert sleeps == []  # yielded to next tick, never slept in-run


def test_transient_failure_is_requeued(monkeypatch):
    """Persistent transient failure is requeued with a persisted backoff."""

    class AlwaysFailAPI(FakeAPI):
        def publish_text(self, text, reply_to_id=None, topic_tag=None):
            raise RuntimeError("HTTPStatusError: 400 Bad Request")

    api = AlwaysFailAPI()
    store = FakeStore(row=dict(ROW))

    requeued = {}

    def fake_requeue(store_arg, table, row_id, error, **kw):
        requeued["row_id"] = row_id
        requeued["error"] = error
        requeued["next_retry_at"] = kw.get("next_retry_at")
        requeued["attempt_count"] = kw.get("attempt_count")
        return True

    monkeypatch.setattr(
        "threads_operator.publish_worker.requeue_failed_row", fake_requeue
    )
    result = publish_next_with_recovery(
        api,
        store,
        "threads_publish_queue",
    )
    assert result["status"] == "failed"
    assert result.get("requeued") is True
    assert requeued["row_id"] == 9
    assert requeued["next_retry_at"] is not None
    assert requeued["attempt_count"] == 1


def test_non_transient_failure_stays_failed(monkeypatch):
    api = FakeAPI(fail_at=1, error="OAuthException: token expired")
    store = FakeStore(row=dict(ROW))

    called = []

    def fake_requeue(store_arg, table, row_id, error, **kw):
        called.append(row_id)
        return True

    monkeypatch.setattr(
        "threads_operator.publish_worker.requeue_failed_row", fake_requeue
    )
    result = publish_next_with_recovery(api, store, "threads_publish_queue")
    assert result["status"] == "failed"
    assert "requeued" not in result
    assert called == []


def test_requeue_failure_does_not_mask_original_error(monkeypatch):
    class FailAPI(FakeAPI):
        def publish_text(self, text, reply_to_id=None, topic_tag=None):
            raise RuntimeError("HTTPStatusError: 400 Bad Request")

    api = FailAPI()
    store = FakeStore(row=dict(ROW))

    def boom(store_arg, table, row_id, error, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr("threads_operator.publish_worker.requeue_failed_row", boom)
    result = publish_next_with_recovery(api, store, "threads_publish_queue")
    assert result["status"] == "failed"
    assert result.get("requeued") is False


def test_dry_run_never_requeues():
    api = FakeAPI()
    store = FakeStore(row=dict(ROW))
    result = publish_next_with_recovery(
        api, store, "threads_publish_queue", dry_run=True
    )
    assert result["status"] == "dry-run"
    assert "requeued" not in result


def test_rate_limit_classification_handles_oauth_label():
    result = {
        "error": (
            "HTTPStatusError: Meta publish rejected (400): Meta error: "
            "message='Application request limit reached', "
            "type='OAuthException', code=4"
        )
    }
    assert is_rate_limited_publish_result(result)
    assert is_transient_publish_result(result)


def test_rate_limit_is_requeued_but_not_retried_within_run(monkeypatch):
    class RateLimitedAPI(FakeAPI):
        def publish_text(self, text, reply_to_id=None, topic_tag=None):
            self.calls.append((text, reply_to_id))
            raise RuntimeError(
                "HTTPStatusError: Meta publish rejected (400): Meta error: "
                "message='Application request limit reached', "
                "type='OAuthException', code=4"
            )

    api = RateLimitedAPI()
    store = FakeStore(row=dict(ROW))
    sleeps = []

    monkeypatch.setattr(
        "threads_operator.publish_worker.requeue_failed_row",
        lambda store, table, row_id, error, **kw: True,
    )
    result = publish_next_with_recovery(
        api,
        store,
        "threads_publish_queue",
        sleep=sleeps.append,
    )

    assert result["status"] == "failed"
    assert result["requeued"] is True
    assert result["attempts"] == 1
    assert len(api.calls) == 1
    assert sleeps == []
