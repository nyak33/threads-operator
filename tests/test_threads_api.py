import json

import httpx
import pytest

from threads_operator.threads_api import ThreadsAPI


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_list_posts_normalizes_owned_posts():
    def handler(request):
        assert request.url.path == "/v1.0/user-123/threads"
        assert request.url.params["access_token"] == "token"
        payload = {
            "data": [
                {
                    "id": "post-1",
                    "text": "hello",
                    "timestamp": "2026-09-09T01:00:00+0000",
                    "media_type": "TEXT",
                    "permalink": "https://www.threads.net/@example/post/abc",
                }
            ]
        }
        return httpx.Response(200, json=payload)

    api = ThreadsAPI("token", "user-123", client=make_client(handler))
    posts = api.list_posts()
    assert posts == [
        {
            "id": "post-1",
            "text": "hello",
            "timestamp": "2026-09-09T01:00:00+0000",
            "media_type": "TEXT",
            "permalink": "https://www.threads.net/@example/post/abc",
        }
    ]


@pytest.mark.parametrize(
    "base_url",
    [
        "http://graph.threads.net/v1.0",
        "https://evil.example/v1.0",
        "https://graph.threads.net.evil.example/v1.0",
    ],
)
def test_threads_api_rejects_non_official_or_insecure_base_urls(base_url):
    with pytest.raises(ValueError, match="official Threads API"):
        ThreadsAPI("token", "user-123", base_url=base_url)


def test_threads_api_accepts_official_https_base_url():
    api = ThreadsAPI("token", "user-123", base_url="https://graph.threads.net/v1.0")
    assert api.base_url == "https://graph.threads.net/v1.0"
    api.client.close()


def test_post_insights_parse_values_and_total_value_and_preserve_missing():
    def handler(request):
        assert request.url.path == "/v1.0/post-1/insights"
        assert "views" in request.url.params["metric"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"name": "views", "values": [{"value": 1200}]},
                    {"name": "likes", "total_value": {"value": 44}},
                    {"name": "replies", "values": [{"value": 7}]},
                    {"name": "reposts", "total_value": {"value": 3}},
                    {"name": "quotes", "values": [{"value": 2}]},
                ]
            },
        )

    api = ThreadsAPI("token", "user-123", client=make_client(handler))
    metrics = api.get_post_insights("post-1")
    assert metrics == {
        "views": 1200,
        "likes": 44,
        "replies": 7,
        "reposts": 3,
        "quotes": 2,
        "shares": None,
    }


def test_account_insights_parse_supported_metrics():
    def handler(request):
        assert request.url.path == "/v1.0/user-123/threads_insights"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"name": "views", "total_value": {"value": 951}},
                    {"name": "likes", "values": [{"value": 27}]},
                    {"name": "replies", "values": [{"value": 8}]},
                    {"name": "reposts", "total_value": {"value": 4}},
                    {"name": "quotes", "values": [{"value": 1}]},
                    {"name": "clicks", "total_value": {"value": 12}},
                    {"name": "followers_count", "total_value": {"value": 206}},
                ]
            },
        )

    api = ThreadsAPI("token", "user-123", client=make_client(handler))
    metrics = api.get_account_insights()
    assert metrics["followers_count"] == 206
    assert metrics["views"] == 951
    assert metrics["clicks"] == 12


def test_account_insights_uses_latest_dated_value():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "views",
                        "values": [
                            {"value": 100, "end_time": "2026-09-09T07:00:00+0000"},
                            {"value": 175, "end_time": "2026-09-10T07:00:00+0000"},
                        ],
                    }
                ]
            },
        )

    api = ThreadsAPI("token", "user-123", client=make_client(handler))
    assert api.get_account_insights()["views"] == 175


def test_non_scalar_metric_is_left_unknown():
    def handler(request):
        return httpx.Response(
            200,
            content=json.dumps({"data": [{"name": "views", "total_value": {"value": {"nested": 1}}}]}),
            headers={"content-type": "application/json"},
        )

    api = ThreadsAPI("token", "user-123", client=make_client(handler))
    assert api.get_account_insights()["views"] is None


def test_metric_request_falls_back_individually_when_combined_request_is_rejected():
    calls = []

    def handler(request):
        metric = request.url.params["metric"]
        calls.append(metric)
        if "," in metric:
            return httpx.Response(400, json={"error": {"message": "unsupported metric combination"}})
        if metric == "views":
            return httpx.Response(200, json={"data": [{"name": "views", "values": [{"value": 99}]}]})
        if metric == "likes":
            return httpx.Response(200, json={"data": [{"name": "likes", "total_value": {"value": 5}}]})
        return httpx.Response(400, json={"error": {"message": "metric unavailable"}})

    api = ThreadsAPI("token", "user-123", client=make_client(handler))
    metrics = api.get_post_insights("post-1")
    assert metrics["views"] == 99
    assert metrics["likes"] == 5
    assert metrics["shares"] is None
    assert calls[0].count(",") >= 1
    assert "views" in calls[1:]


def test_oauth_400_is_not_silently_treated_as_missing_metrics():
    def handler(request):
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
        api.get_post_insights("post-1")


def test_unknown_publish_400_is_preserved_as_retryable_diagnostic_error():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/threads"):
            return httpx.Response(200, json={"id": "container-1"})
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Unexpected publish failure",
                    "type": "GraphMethodException",
                    "code": 100,
                    "error_subcode": 2207026,
                    "fbtrace_id": "trace-abc",
                }
            },
        )

    api = ThreadsAPI("token", "user-123", client=make_client(handler))
    api.__dict__["_publishing_allowed"] = True

    with pytest.raises(Exception) as exc_info:
        api.publish_text("hello", max_attempts=1)

    exc = exc_info.value
    assert getattr(exc, "retryable", None) is True
    assert getattr(exc, "status_code", None) == 400
    diagnostics = getattr(exc, "diagnostics", {})
    assert diagnostics["error_code"] == 100
    assert diagnostics["error_subcode"] == 2207026
    assert diagnostics["fbtrace_id"] == "trace-abc"
    assert diagnostics["classification"] == "unknown_400"


def test_oauth_publish_400_is_classified_permanent():
    def handler(request):
        if request.url.path.endswith("/threads"):
            return httpx.Response(200, json={"id": "container-1"})
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Invalid OAuth access token.",
                    "type": "OAuthException",
                    "code": 190,
                    "fbtrace_id": "trace-oauth",
                }
            },
        )

    api = ThreadsAPI("token", "user-123", client=make_client(handler))
    api.__dict__["_publishing_allowed"] = True

    with pytest.raises(Exception) as exc_info:
        api.publish_text("hello", max_attempts=1)

    exc = exc_info.value
    assert getattr(exc, "retryable", None) is False
    assert getattr(exc, "diagnostics", {})["classification"] == "permanent"


def test_publish_is_blocked_without_explicit_production_enable():
    called = False

    def handler(request):
        nonlocal called
        called = True
        return httpx.Response(200, json={"id": "post-1"})

    api = ThreadsAPI("token", "user-123", client=make_client(handler))
    api.__dict__["_publishing_allowed"] = False

    with pytest.raises(PermissionError, match="publishing is disabled"):
        api.publish_container("container-1")

    assert called is False
