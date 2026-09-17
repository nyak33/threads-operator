from datetime import datetime, timezone

from threads_operator.publisher import publish_next


NOW = datetime(2026, 9, 17, 6, 1, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self, row=None):
        self.row = row
        self.calls = []

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

    def mark_post_retrying(
        self,
        table,
        row_id,
        error,
        diagnostics,
        next_retry_at,
        retry_deadline_at,
        attempt_count,
    ):
        self.calls.append(
            (
                "retrying",
                table,
                row_id,
                error,
                diagnostics,
                next_retry_at,
                retry_deadline_at,
                attempt_count,
            )
        )

    def mark_post_needs_attention(self, table, row_id, error, diagnostics=None):
        self.calls.append(("needs-attention", table, row_id, error, diagnostics))

    def mark_post_failed(self, table, row_id, error):
        self.calls.append(("failed", table, row_id, error))


class FakeAPI:
    def __init__(self, ids=None, fail_at=None, fail_exc=None):
        self.ids = list(ids or [])
        self.fail_at = fail_at
        self.fail_exc = fail_exc
        self.calls = []

    def publish_text(self, text, reply_to_id=None):
        self.calls.append((text, reply_to_id))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            if self.fail_exc is not None:
                raise self.fail_exc
            raise RuntimeError("publish exploded")
        return self.ids.pop(0)


class RetryablePublishError(RuntimeError):
    retryable = True
    diagnostics = {
        "http_status": 400,
        "error_code": 1,
        "error_subcode": 99,
        "fbtrace_id": "trace-123",
        "classification": "unknown_400",
    }


class PermanentPublishError(RuntimeError):
    retryable = False
    diagnostics = {
        "http_status": 400,
        "error_type": "OAuthException",
        "error_code": 190,
        "classification": "permanent",
    }


def row(replies=None):
    return {
        "id": 7,
        "main_post_text": "main post",
        "reply_texts": replies or [],
        "scheduled_at": "2026-09-17T06:00:00+00:00",
        "attempt_count": 0,
    }


def test_dry_run_peeks_without_claiming_or_publishing():
    store = FakeStore(row())
    api = FakeAPI(["post-1"])

    result = publish_next(api, store, "queue", campaign_code="RANDOM", dry_run=True)

    assert result == {"status": "dry-run", "queue_id": 7}
    assert store.calls == [("peek", "queue", "RANDOM")]
    assert api.calls == []


def test_no_due_row_returns_idle():
    store = FakeStore(None)
    api = FakeAPI()

    assert publish_next(api, store, "queue") == {"status": "idle"}
    assert api.calls == []


def test_main_only_publish_claims_before_api_and_marks_posted():
    store = FakeStore(row())
    api = FakeAPI(["post-1"])

    result = publish_next(api, store, "queue")

    assert result == {
        "status": "posted",
        "queue_id": 7,
        "main_post_id": "post-1",
        "reply_ids": [],
    }
    assert store.calls[0][0] == "claim"
    assert api.calls == [("main post", None)]
    assert store.calls[1] == ("main", "queue", 7, "post-1")
    assert store.calls[2] == ("posted", "queue", 7, [])


def test_reply_chain_uses_previous_post_as_parent_and_persists_progress():
    store = FakeStore(row(["reply one", "reply two"]))
    api = FakeAPI(["post-1", "reply-1", "reply-2"])

    result = publish_next(api, store, "queue")

    assert api.calls == [
        ("main post", None),
        ("reply one", "post-1"),
        ("reply two", "reply-1"),
    ]
    assert result["reply_ids"] == ["reply-1", "reply-2"]
    progress = [call for call in store.calls if call[0] == "reply-progress"]
    assert progress == [
        ("reply-progress", "queue", 7, ["reply-1"]),
        ("reply-progress", "queue", 7, ["reply-1", "reply-2"]),
    ]


def test_failure_after_main_publish_retains_main_id_and_marks_failed():
    store = FakeStore(row(["reply one"]))
    api = FakeAPI(["post-1"], fail_at=2)

    result = publish_next(api, store, "queue")

    assert result["status"] == "failed"
    assert result["queue_id"] == 7
    assert result["main_post_id"] == "post-1"
    assert "publish exploded" in result["error"]
    assert ("main", "queue", 7, "post-1") in store.calls
    failed = [call for call in store.calls if call[0] == "failed"]
    assert len(failed) == 1
    assert "publish exploded" in failed[0][3]


def test_failure_after_one_reply_keeps_successful_reply_id_persisted():
    store = FakeStore(row(["reply one", "reply two"]))
    api = FakeAPI(["post-1", "reply-1"], fail_at=3)

    result = publish_next(api, store, "queue")

    assert result["status"] == "failed"
    assert result["main_post_id"] == "post-1"
    assert result["reply_ids"] == ["reply-1"]
    assert ("reply-progress", "queue", 7, ["reply-1"]) in store.calls
    assert store.calls.index(("reply-progress", "queue", 7, ["reply-1"])) < next(
        index for index, call in enumerate(store.calls) if call[0] == "failed"
    )


def test_retryable_main_publish_failure_is_requeued_instead_of_terminal_failed():
    store = FakeStore(row())
    api = FakeAPI(fail_at=1, fail_exc=RetryablePublishError("temporary Meta failure"))

    result = publish_next(api, store, "queue", now=NOW)

    assert result["status"] == "retrying"
    assert result["queue_id"] == 7
    retrying = [call for call in store.calls if call[0] == "retrying"]
    assert len(retrying) == 1
    assert retrying[0][7] == 1
    assert retrying[0][4]["classification"] == "unknown_400"
    assert not [call for call in store.calls if call[0] == "failed"]


def test_permanent_main_publish_failure_requires_attention_without_retrying():
    store = FakeStore(row())
    api = FakeAPI(fail_at=1, fail_exc=PermanentPublishError("invalid token"))

    result = publish_next(api, store, "queue", now=NOW)

    assert result["status"] == "needs_attention"
    attention = [call for call in store.calls if call[0] == "needs-attention"]
    assert len(attention) == 1
    assert attention[0][4]["error_code"] == 190
    assert not [call for call in store.calls if call[0] == "retrying"]


def test_retryable_failure_after_30_minute_deadline_requires_attention():
    retry_row = row()
    retry_row["retry_deadline_at"] = "2026-09-17T06:30:00+00:00"
    retry_row["attempt_count"] = 5
    store = FakeStore(retry_row)
    api = FakeAPI(fail_at=1, fail_exc=RetryablePublishError("temporary Meta failure"))

    result = publish_next(
        api,
        store,
        "queue",
        now=datetime(2026, 9, 17, 6, 30, tzinfo=timezone.utc),
    )

    assert result["status"] == "needs_attention"
    assert not [call for call in store.calls if call[0] == "retrying"]
    assert len([call for call in store.calls if call[0] == "needs-attention"]) == 1
