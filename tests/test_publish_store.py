from pathlib import Path
import json

import httpx

from threads_operator.supabase_store import SupabaseStore

NOW = "2026-09-15T04:00:00+00:00"


def test_peek_due_post_is_scoped_to_account_and_optional_campaign():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/rest/v1/custom_queue"
        params = request.url.params
        assert params["account_key"] == "eq.brand_a"
        assert params["status"] == "eq.approved"
        assert params["scheduled_at"] == f"lte.{NOW}"
        assert params["campaign_code"] == "eq.RANDOM_LIFE"
        assert params["order"] == "scheduled_at.asc,id.asc"
        assert params["limit"] == "1"
        return httpx.Response(200, json=[{"id": 7, "main_post_text": "hello"}])

    store = SupabaseStore(
        "https://example.supabase.co",
        "secret",
        account_key="brand_a",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    row = store.peek_due_post("custom_queue", campaign_code="RANDOM_LIFE", now=NOW)
    assert row["id"] == 7


def test_claim_due_post_uses_conditional_patch_and_returns_claimed_row():
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 7, "main_post_text": "hello"}])
        if request.method == "PATCH":
            params = request.url.params
            assert params["id"] == "eq.7"
            assert params["account_key"] == "eq.brand_a"
            assert params["status"] == "eq.approved"
            body = json.loads(request.read().decode())
            assert body["status"] == "posting"
            assert body["claimed_at"] == NOW
            assert request.headers["prefer"] == "return=representation"
            return httpx.Response(
                200,
                json=[{"id": 7, "status": "posting", "main_post_text": "hello"}],
            )
        raise AssertionError(request.method)

    store = SupabaseStore(
        "https://example.supabase.co",
        "secret",
        account_key="brand_a",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    claimed = store.claim_due_post("custom_queue", now=NOW)
    assert claimed["status"] == "posting"
    assert [request.method for request in calls] == ["GET", "PATCH"]


def test_claim_due_post_returns_none_when_another_worker_won():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 7, "main_post_text": "hello"}])
        return httpx.Response(200, json=[])

    store = SupabaseStore(
        "https://example.supabase.co",
        "secret",
        account_key="brand_a",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert store.claim_due_post("custom_queue", now=NOW) is None


def test_mark_post_states_remain_account_scoped():
    patches = []

    def handler(request):
        assert request.method == "PATCH"
        assert request.url.params["account_key"] == "eq.brand_a"
        patches.append(json.loads(request.read().decode()))
        return httpx.Response(204)

    store = SupabaseStore(
        "https://example.supabase.co",
        "secret",
        account_key="brand_a",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    store.mark_post_main_published("custom_queue", 7, "post-1")
    store.mark_post_reply_progress("custom_queue", 7, ["reply-1"])
    store.mark_post_posted("custom_queue", 7, ["reply-1"], posted_at=NOW)
    store.mark_post_failed("custom_queue", 8, "boom")

    assert patches[0] == {"threads_main_post_id": "post-1"}
    assert patches[1] == {"threads_reply_ids": ["reply-1"]}
    assert patches[2]["status"] == "posted"
    assert patches[2]["threads_reply_ids"] == ["reply-1"]
    assert patches[2]["posted_at"] == NOW
    assert patches[2]["last_error"] is None
    assert patches[3] == {"status": "failed", "last_error": "boom"}


def test_publish_queue_migration_has_account_and_safety_constraints():
    sql = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "004_threads_publish_queue.sql"
    ).read_text().lower()

    assert "create table if not exists public.threads_publish_queue" in sql
    assert "account_key text not null" in sql
    assert "reply_texts jsonb" in sql
    assert "status text not null default 'draft'" in sql
    assert "'approved'" in sql and "'posting'" in sql and "'posted'" in sql and "'failed'" in sql
    assert "enable row level security" in sql
    assert "service_role" in sql
