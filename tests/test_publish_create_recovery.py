import httpx
import pytest

from threads_operator.threads_api import ThreadsAPI


def test_container_creation_503_is_retryable_for_queue_recovery():
    def handler(request):
        assert request.url.path.endswith("/threads")
        return httpx.Response(
            503,
            json={
                "error": {
                    "message": "Service temporarily unavailable",
                    "type": "ServerException",
                    "code": 2,
                    "fbtrace_id": "trace-create-503",
                }
            },
        )

    api = ThreadsAPI(
        "token",
        "user-123",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    api.enable_publishing()

    with pytest.raises(Exception) as exc_info:
        api.publish_text("hello", max_attempts=1)

    exc = exc_info.value
    assert getattr(exc, "retryable", None) is True
    diagnostics = getattr(exc, "diagnostics", {})
    assert diagnostics["http_status"] == 503
    assert diagnostics["classification"] == "temporary"
    assert diagnostics["fbtrace_id"] == "trace-create-503"
