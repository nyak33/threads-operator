from pathlib import Path
import json

import httpx

from threads_operator.supabase_store import SupabaseStore


def test_activity_store_scopes_fingerprint_lookup_and_insert_by_account_key():
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            assert request.url.params.get("account_key") == "eq.brand_a"
            assert request.url.params.get("fallback_fingerprint") == "in.(same-fp)"
            return httpx.Response(200, json=[])
        if request.method == "POST":
            payload = json.loads(request.read().decode())
            assert payload == [
                {
                    "notification_id": "n1",
                    "fallback_fingerprint": "same-fp",
                    "account_key": "brand_a",
                }
            ]
            return httpx.Response(201, json=[])
        raise AssertionError(request.method)

    store = SupabaseStore(
        "https://example.supabase.co",
        "secret",
        account_key="brand_a",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    events = [{"notification_id": "n1", "fallback_fingerprint": "same-fp"}]

    assert store.insert_activity_events(events) == 1
    assert events[0]["upserted"] is True
    assert len(calls) == 2


def test_same_activity_fingerprint_can_exist_for_two_accounts():
    seen_accounts = []

    def handler(request):
        if request.method == "GET":
            seen_accounts.append(request.url.params.get("account_key"))
            return httpx.Response(200, json=[])
        return httpx.Response(201, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    for account in ("brand_a", "brand_b"):
        store = SupabaseStore(
            "https://example.supabase.co",
            "secret",
            account_key=account,
            client=client,
        )
        store.insert_activity_events(
            [{"notification_id": f"n-{account}", "fallback_fingerprint": "same-fp"}]
        )

    assert seen_accounts == ["eq.brand_a", "eq.brand_b"]


def test_activity_migration_is_account_scoped():
    sql = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "003_activity_follow_events.sql"
    ).read_text().lower()

    assert "account_key text not null" in sql
    assert "unique (account_key, fallback_fingerprint)" in sql
    assert "group by account_key, matched_post_id" in sql
