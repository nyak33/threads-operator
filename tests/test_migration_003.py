"""Tests for the Activity follow-attribution migration.

Live DB mutation tests are opt-in only: set RUN_LIVE_DB_TESTS=1 and point the
Supabase variables at a disposable/test project. Normal CI performs static
schema-contract checks only and never mutates production.
"""
from __future__ import annotations

import os
from pathlib import Path
import uuid

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "003_activity_follow_events.sql"
TABLE_EVENTS = "threads_activity_events"


def migration_sql() -> str:
    return MIGRATION.read_text().lower()


def test_activity_table_is_append_only_by_privilege_contract():
    sql = migration_sql()
    assert "grant select, insert on table public.threads_activity_events to service_role" in sql
    assert "grant select, insert, update" not in sql
    assert "grant select, insert, update, delete" not in sql
    assert "collector never updates or deletes" in sql


def test_activity_table_dedupes_observation_state_per_account():
    sql = migration_sql()
    assert "fallback_fingerprint text not null" in sql
    assert "unique (account_key, fallback_fingerprint)" in sql
    assert "account_key text not null" in sql
    assert "notification_id text not null" in sql
    assert "notification_id text not null unique" not in sql


def test_summary_is_rebuildable_per_account_and_post_latest_state_view():
    sql = migration_sql()
    assert "create or replace view public.threads_follows_from_post_summary" in sql
    assert "security_invoker" in sql
    assert "distinct on (account_key, notification_id)" in sql
    assert "follows_from_post_activity" in sql
    assert "group by account_key, matched_post_id" in sql


def test_migration_stores_matched_post_id_without_external_posts_dependency():
    sql = migration_sql()
    assert "matched_post_id text" in sql
    assert "references public.threads_posts" not in sql
    assert "threads_known_posts" not in sql


def test_activity_table_has_rls_and_no_public_access():
    sql = migration_sql()
    assert "alter table public.threads_activity_events enable row level security" in sql
    assert "revoke all on table public.threads_activity_events from public, anon, authenticated" in sql


@pytest.fixture
def live_db() -> dict[str, str]:
    if os.environ.get("RUN_LIVE_DB_TESTS") != "1":
        pytest.skip("set RUN_LIVE_DB_TESTS=1 explicitly for disposable DB tests")
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        pytest.skip("SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY required")
    return {"url": url.rstrip("/"), "key": key}


def _headers(key: str, prefer: str = "return=minimal") -> dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def _event() -> dict:
    token = uuid.uuid4().hex
    return {
        "account_key": "fixture_account",
        "notification_id": token,
        "notification_datetime": "2026-09-12T10:30:00+00:00",
        "notification_type": "follow",
        "visible_username": "fixture_user",
        "source_snippet_raw": "Fixture source post",
        "source_snippet_normalized": "fixture source post",
        "context_raw": "Followed from your post",
        "grouped_others_count": 0,
        "follow_count": 1,
        "attribution_possible": True,
        "match_confidence": "unknown",
        "fallback_fingerprint": token,
    }


def test_live_insert_and_duplicate_fingerprint_rejected_within_account(live_db):
    evt = _event()
    url = f"{live_db['url']}/rest/v1/{TABLE_EVENTS}"
    with httpx.Client() as client:
        first = client.post(url, headers=_headers(live_db["key"]), json=evt)
        assert first.status_code == 201, first.text
        duplicate = client.post(url, headers=_headers(live_db["key"]), json=evt)
        assert duplicate.status_code in (400, 409), duplicate.text
