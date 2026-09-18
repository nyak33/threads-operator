from threads_operator.publish_worker import (
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

    def publish_text(self, text, reply_to_id=None):
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


def test_transient_failure_is_requeued(monkeypatch):
    api = FakeAPI(fail_at=1, error="HTTPStatusError: 400 Bad Request")
    store = FakeStore(row=dict(ROW))

    requeued = {}

    def fake_requeue(store_arg, table, row_id, error):
        requeued["row_id"] = row_id
        requeued["error"] = error
        return True

    monkeypatch.setattr(
        "threads_operator.publish_worker.requeue_failed_row", fake_requeue
    )
    result = publish_next_with_recovery(api, store, "threads_publish_queue")
    assert result["status"] == "failed"
    assert result.get("requeued") is True
    assert requeued["row_id"] == 9


def test_non_transient_failure_stays_failed(monkeypatch):
    api = FakeAPI(fail_at=1, error="OAuthException: token expired")
    store = FakeStore(row=dict(ROW))

    called = []

    def fake_requeue(store_arg, table, row_id, error):
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
    api = FakeAPI(fail_at=1, error="HTTPStatusError: 400 Bad Request")
    store = FakeStore(row=dict(ROW))

    def boom(store_arg, table, row_id, error):
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
