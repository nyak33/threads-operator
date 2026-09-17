from datetime import datetime, timezone

from threads_operator.publisher import publish_next


class RetryableError(RuntimeError):
    retryable = True
    diagnostics = {
        "http_status": 400,
        "error_message": "temporary publish failure",
        "error_type": "GraphMethodException",
        "error_code": 100,
        "error_subcode": 2207026,
        "fbtrace_id": "trace-123",
        "classification": "unknown_400",
        "container_id": "container-123",
    }


class API:
    def publish_text(self, text, reply_to_id=None):
        raise RetryableError("temporary publish failure")


class Store:
    account_key = "syaqir"

    def __init__(self):
        self.retry = None

    def claim_due_post(self, table, campaign_code=None, now=None):
        return {
            "id": 77,
            "status": "posting",
            "campaign_code": "RANDOM_LIFE",
            "main_post_text": "hello",
            "reply_texts": [],
            "scheduled_at": "2026-09-17T06:00:00+00:00",
            "attempt_count": 2,
        }

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
        self.retry = {
            "row_id": row_id,
            "diagnostics": diagnostics,
            "next_retry_at": next_retry_at,
            "retry_deadline_at": retry_deadline_at,
            "attempt_count": attempt_count,
        }

    def mark_post_needs_attention(self, *args, **kwargs):
        raise AssertionError("should remain retryable")

    def mark_post_failed(self, *args, **kwargs):
        raise AssertionError("should remain retryable")


def test_retry_diagnostics_include_queue_context_and_attempt_metadata():
    store = Store()
    now = datetime(2026, 9, 17, 6, 5, tzinfo=timezone.utc)

    result = publish_next(
        API(),
        store,
        "threads_publish_queue",
        campaign_code="RANDOM_LIFE",
        now=now,
    )

    assert result["status"] == "retrying"
    diagnostics = store.retry["diagnostics"]
    assert diagnostics["account_key"] == "syaqir"
    assert diagnostics["campaign_code"] == "RANDOM_LIFE"
    assert diagnostics["queue_row_id"] == 77
    assert diagnostics["scheduled_at"] == "2026-09-17T06:00:00+00:00"
    assert diagnostics["attempt_number"] == 3
    assert diagnostics["timestamp"] == "2026-09-17T06:05:00+00:00"
    assert diagnostics["next_retry_at"] == store.retry["next_retry_at"]
    assert diagnostics["retry_deadline_at"] == store.retry["retry_deadline_at"]
