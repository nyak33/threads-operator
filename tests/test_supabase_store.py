import httpx

from threads_operator.supabase_store import SupabaseStore


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_insert_account_snapshot_is_append_only_post():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(201, json=[])

    store = SupabaseStore("https://example.supabase.co", "secret", client=make_client(handler))
    store.insert_account_snapshot({"account_id": "acct", "captured_at": "2026-09-09T00:00:00+00:00"})
    assert seen == {
        "method": "POST",
        "path": "/rest/v1/threads_account_snapshots",
        "auth": "Bearer secret",
    }


def test_insert_post_snapshot_is_append_only_post():
    methods = []

    def handler(request):
        methods.append((request.method, request.url.path))
        return httpx.Response(201, json=[])

    store = SupabaseStore("https://example.supabase.co", "secret", client=make_client(handler))
    store.insert_post_snapshot({"post_id": "p1", "captured_at": "2026-09-09T00:00:00+00:00"})
    assert methods == [("POST", "/rest/v1/threads_post_snapshots")]


def test_latest_post_snapshot_orders_desc_and_limits_one():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/rest/v1/threads_post_snapshots"
        assert request.url.params["post_id"] == "eq.p1"
        assert request.url.params["order"] == "captured_at.desc"
        assert request.url.params["limit"] == "1"
        return httpx.Response(200, json=[{"post_id": "p1", "captured_at": "2026-09-09T00:00:00+00:00"}])

    store = SupabaseStore("https://example.supabase.co", "secret", client=make_client(handler))
    row = store.latest_post_snapshot("p1")
    assert row["post_id"] == "p1"


def test_latest_post_snapshot_returns_none_for_no_history():
    def handler(request):
        return httpx.Response(200, json=[])

    store = SupabaseStore("https://example.supabase.co", "secret", client=make_client(handler))
    assert store.latest_post_snapshot("missing") is None


def test_latest_account_snapshot_orders_desc_and_limits_one():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/rest/v1/threads_account_snapshots"
        assert request.url.params["account_id"] == "eq.user-123"
        assert request.url.params["order"] == "captured_at.desc"
        assert request.url.params["limit"] == "1"
        return httpx.Response(200, json=[{"account_id": "user-123", "captured_at": "2026-09-09T00:00:00+00:00"}])

    store = SupabaseStore("https://example.supabase.co", "secret", client=make_client(handler))
    row = store.latest_account_snapshot("user-123")
    assert row["account_id"] == "user-123"
