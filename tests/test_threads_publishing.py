from urllib.parse import parse_qs

import httpx
import pytest

from threads_operator.threads_api import ThreadsAPI


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def form_body(request):
    return {key: values[-1] for key, values in parse_qs(request.read().decode()).items()}


def test_create_text_container_posts_form_encoded_text():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/v1.0/user-123/threads"
        assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
        assert form_body(request) == {
            "access_token": "token",
            "media_type": "TEXT",
            "text": "hello world",
        }
        return httpx.Response(200, json={"id": "creation-1"})

    api = ThreadsAPI("token", "user-123", client=make_client(handler))
    assert api.create_text_container("hello world") == "creation-1"


def test_create_text_container_supports_reply_parent():
    def handler(request):
        assert form_body(request)["reply_to_id"] == "parent-123"
        return httpx.Response(200, json={"id": "creation-reply"})

    api = ThreadsAPI("token", "user-123", client=make_client(handler))
    assert api.create_text_container("reply", reply_to_id="parent-123") == "creation-reply"


def test_publish_container_posts_creation_id():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/v1.0/user-123/threads_publish"
        assert form_body(request) == {
            "access_token": "token",
            "creation_id": "creation-1",
        }
        return httpx.Response(200, json={"id": "post-1"})

    api = ThreadsAPI("token", "user-123", client=make_client(handler))
    assert api.publish_container("creation-1") == "post-1"


def test_create_or_publish_requires_returned_id():
    api = ThreadsAPI(
        "token",
        "user-123",
        client=make_client(lambda request: httpx.Response(200, json={})),
    )
    with pytest.raises(ValueError, match="id"):
        api.create_text_container("hello")


def test_publish_text_retries_transient_processing_error():
    publish_attempts = 0

    def handler(request):
        nonlocal publish_attempts
        if request.url.path.endswith("/threads"):
            return httpx.Response(200, json={"id": "creation-1"})
        publish_attempts += 1
        if publish_attempts == 1:
            return httpx.Response(
                400,
                json={"error": {"message": "Media is still processing and not ready for publishing"}},
            )
        return httpx.Response(200, json={"id": "post-1"})

    api = ThreadsAPI("token", "user-123", client=make_client(handler))
    assert api.publish_text("hello", max_attempts=2, retry_delay_seconds=0) == "post-1"
    assert publish_attempts == 2


def test_publish_text_recreates_container_once_after_persistent_400():
    created = 0
    publish_attempts = 0

    def handler(request):
        nonlocal created, publish_attempts
        if request.url.path.endswith("/threads"):
            created += 1
            return httpx.Response(200, json={"id": f"creation-{created}"})
        publish_attempts += 1
        if publish_attempts == 1:
            return httpx.Response(400, json={"error": {"message": "Bad request"}})
        return httpx.Response(200, json={"id": "post-2"})

    api = ThreadsAPI("token", "user-123", client=make_client(handler))
    assert api.publish_text("hello", max_attempts=1, retry_delay_seconds=0) == "post-2"
    assert created == 2
    assert publish_attempts == 2


def test_publish_text_no_container_retry_when_container_retries_zero():
    publish_attempts = 0

    def handler(request):
        nonlocal publish_attempts
        if request.url.path.endswith("/threads"):
            return httpx.Response(200, json={"id": "creation-1"})
        publish_attempts += 1
        return httpx.Response(400, json={"error": {"message": "Bad request"}})

    api = ThreadsAPI("token", "user-123", client=make_client(handler))
    with pytest.raises(httpx.HTTPStatusError):
        api.publish_text("hello", max_attempts=1, retry_delay_seconds=0, container_retries=0)
    assert publish_attempts == 1


def test_publish_text_does_not_retry_oauth_failure():
    publish_attempts = 0

    def handler(request):
        nonlocal publish_attempts
        if request.url.path.endswith("/threads"):
            return httpx.Response(200, json={"id": "creation-1"})
        publish_attempts += 1
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Invalid OAuth access token.",
                    "type": "OAuthException",
                    "code": 190,
                }
            },
        )

    api = ThreadsAPI("token", "user-123", client=make_client(handler))
    with pytest.raises(httpx.HTTPStatusError):
        api.publish_text("hello", max_attempts=4, retry_delay_seconds=0)
    assert publish_attempts == 1


def test_publish_text_rate_limit_yields_without_container_retry():
    created = 0
    publish_attempts = 0

    def handler(request):
        nonlocal created, publish_attempts
        if request.url.path.endswith("/threads"):
            created += 1
            return httpx.Response(200, json={"id": f"creation-{created}"})
        publish_attempts += 1
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Application request limit reached",
                    "type": "OAuthException",
                    "code": 4,
                }
            },
        )

    api = ThreadsAPI("token", "user-123", client=make_client(handler))
    with pytest.raises(httpx.HTTPStatusError):
        api.publish_text("hello", max_attempts=4, retry_delay_seconds=0)

    assert created == 1
    assert publish_attempts == 1


def test_publish_text_permission_error_does_not_retry():
    created = 0
    publish_attempts = 0

    def handler(request):
        nonlocal created, publish_attempts
        if request.url.path.endswith("/threads"):
            created += 1
            return httpx.Response(200, json={"id": f"creation-{created}"})
        publish_attempts += 1
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Application does not have permission for this action",
                    "type": "GraphMethodException",
                    "code": 10,
                }
            },
        )

    api = ThreadsAPI("token", "user-123", client=make_client(handler))
    with pytest.raises(httpx.HTTPStatusError):
        api.publish_text("hello", max_attempts=4, retry_delay_seconds=0)

    assert created == 1
    assert publish_attempts == 1
