import httpx
import pytest

from threads_operator.threads_api import ThreadsAPI


def test_publish_transport_failure_is_retryable_for_queue_recovery():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if request.url.path.endswith("/threads"):
            return httpx.Response(200, json={"id": "container-1"})
        raise httpx.ConnectError("temporary connection failure", request=request)

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
    assert getattr(exc, "diagnostics", {})["classification"] == "transport"
    assert calls == 2
