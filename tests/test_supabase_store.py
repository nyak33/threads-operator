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
        "path": "/rest/v1/threads_account_insights_snapshots",
        "auth": "Bearer secret",
    }


def test_insert_post_snapshot_is_append_only_post():
    methods = []

    def handler(request):
        methods.append((request.method, request.url.path))
        return httpx.Response(201, json=[])

    store = SupabaseStore("https://example.supabase.co", "secret", client=make_client(handler))
    store.insert_post_snapshot({"post_id": "p1", "captured_at": "2026-09-09T00:00:00+00:00"})
    assert methods == [("POST", "/rest/v1/threads_post_insights_snapshots")]


def test_latest_post_snapshot_orders_desc_and_limits_one():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/rest/v1/threads_post_insights_snapshots"
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
        assert request.url.path == "/rest/v1/threads_account_insights_snapshots"
        assert request.url.params["account_id"] == "eq.user-123"
        assert request.url.params["order"] == "captured_at.desc"
        assert request.url.params["limit"] == "1"
        return httpx.Response(200, json=[{"account_id": "user-123", "captured_at": "2026-09-09T00:00:00+00:00"}])

    store = SupabaseStore("https://example.supabase.co", "secret", client=make_client(handler))
    row = store.latest_account_snapshot("user-123")
    assert row["account_id"] == "user-123"


def test_latest_post_snapshots_fetches_recent_rows_in_one_batch():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.path == "/rest/v1/threads_post_insights_snapshots"
        assert request.url.params["captured_at"] == "gte.2026-09-11T07:00:00+00:00"
        assert request.url.params["order"] == "captured_at.desc"
        assert request.url.params["select"].startswith("post_id,captured_at,views")
        return httpx.Response(
            200,
            json=[
                {"post_id": "p1", "captured_at": "2026-09-12T07:30:00+00:00", "views": 5},
                {"post_id": "p2", "captured_at": "2026-09-12T07:20:00+00:00", "views": 8},
            ],
        )

    store = SupabaseStore("https://example.supabase.co", "secret", client=make_client(handler))
    latest = store.latest_post_snapshots("2026-09-11T07:00:00+00:00")

    assert set(latest) == {"p1", "p2"}
    assert len(seen) == 1


def test_latest_post_snapshots_ignores_newer_all_null_row():
    def handler(request):
        return httpx.Response(
            200,
            json=[
                {
                    "post_id": "p1",
                    "captured_at": "2026-09-12T07:59:00+00:00",
                    "views": None,
                    "likes": None,
                    "replies": None,
                    "reposts": None,
                    "quotes": None,
                    "shares": None,
                },
                {
                    "post_id": "p1",
                    "captured_at": "2026-09-12T07:30:00+00:00",
                    "views": 5,
                    "likes": None,
                    "replies": None,
                    "reposts": None,
                    "quotes": None,
                    "shares": None,
                },
            ],
        )

    store = SupabaseStore("https://example.supabase.co", "secret", client=make_client(handler))
    latest = store.latest_post_snapshots("2026-09-11T07:00:00+00:00")

    assert latest["p1"]["captured_at"] == "2026-09-12T07:30:00+00:00"


def test_latest_post_snapshots_omits_post_with_only_all_null_history():
    def handler(request):
        return httpx.Response(
            200,
            json=[
                {
                    "post_id": "p1",
                    "captured_at": "2026-09-12T07:59:00+00:00",
                    "views": None,
                    "likes": None,
                    "replies": None,
                    "reposts": None,
                    "quotes": None,
                    "shares": None,
                }
            ],
        )

    store = SupabaseStore("https://example.supabase.co", "secret", client=make_client(handler))
    latest = store.latest_post_snapshots("2026-09-11T07:00:00+00:00")

    assert latest == {}


def test_latest_post_snapshots_paginates_large_result_sets():
    offsets = []

    def handler(request):
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        if offset == 0:
            return httpx.Response(
                200,
                json=[
                    {"post_id": "p1", "captured_at": "2026-09-12T07:30:00+00:00", "views": 5},
                    {"post_id": "p2", "captured_at": "2026-09-12T07:20:00+00:00", "views": 8},
                ],
            )
        return httpx.Response(
            200,
            json=[{"post_id": "p3", "captured_at": "2026-09-12T07:10:00+00:00", "views": 3}],
        )

    store = SupabaseStore("https://example.supabase.co", "secret", client=make_client(handler))
    latest = store.latest_post_snapshots("2026-09-11T07:00:00+00:00", page_size=2)

    assert offsets == [0, 2]
    assert set(latest) == {"p1", "p2", "p3"}
