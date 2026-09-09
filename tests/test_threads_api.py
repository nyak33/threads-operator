import json

import httpx

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
