"""Regression test: fresh-install RLS/privilege security posture (migration 014).

Defect (externally verified 2026-09-23): replaying migrations 013..012 on a
completely fresh Supabase project did NOT reproduce production's Row-Level
Security posture. RLS stayed disabled and default privileges remained on the
7 operator tables, and Supabase Security Advisor flagged
``threads_own_reply_engagement`` as public-but-RLS-disabled.

Migration 014 reconciles this. This test asserts the migration *actually
contains* the required posture for every affected table so it cannot silently
regress (e.g. a table dropped from the list, or a grant accidentally widened).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MIGRATION = (REPO / "migrations" / "014_fresh_schema_security_reconcile.sql").read_text()

# table -> exact service_role privilege set 014 must grant (mirrors 013 + Workflow B)
EXPECTED = {
    "threads_trend_candidates": "grant select, insert, update, delete",
    "threads_engagement_config": "grant select, insert, update",
    "threads_gateway_keys": "grant select, insert, update, delete",
    "threads_posts": "grant select, insert, update",
    "threads_inbound_replies": "grant select, insert, update",
    "threads_post_metric_snapshots": "grant select, insert",
    "threads_own_reply_engagement": "grant select, insert, update",
}

# normalize whitespace so line-wrapping in the SQL doesn't break matching
NORM = re.sub(r"\s+", " ", MIGRATION)


def _section_for(table: str) -> str:
    """Return the migration text from this table's guard to the next guard/end."""
    start = NORM.index(f"to_regclass('public.{table}')")
    nxt = NORM.find("to_regclass('public.", start + 1)
    return NORM[start: nxt if nxt != -1 else len(NORM)]


def test_all_seven_tables_are_reconciled():
    for table in EXPECTED:
        assert f"to_regclass('public.{table}')" in MIGRATION, f"{table} missing from 014"


def test_rls_enabled_on_every_table():
    for table in EXPECTED:
        assert f"alter table public.{table} enable row level security" in _section_for(table), \
            f"RLS not enabled on {table}"


def test_client_roles_revoked_on_every_table():
    for table in EXPECTED:
        sec = _section_for(table)
        assert f"revoke all on table public.{table} from public" in sec, f"public not revoked on {table}"
        assert f"revoke all on table public.{table} from anon, authenticated" in sec, \
            f"anon/authenticated not revoked on {table}"


def test_service_role_grants_match_production():
    for table, grant in EXPECTED.items():
        assert f"{grant} on table public.{table} to service_role" in _section_for(table), \
            f"service_role grant mismatch on {table} (expected '{grant}')"


def test_service_role_policy_created_idempotently():
    for table in EXPECTED:
        sec = _section_for(table)
        assert f'drop policy if exists "service_role_all_{table}" on public.{table}' in sec
        assert re.search(
            rf'create policy "service_role_all_{table}" on public\.{table} for all to service_role using \(true\) with check \(true\)',
            sec,
        ), f"service_role policy missing on {table}"


def test_no_client_facing_policies_created():
    # The operator is service_role-only. 014 must NOT create permissive
    # anon/authenticated policies. Only 'service_role_all_*' policies allowed.
    created = re.findall(r'create policy "([^"]+)"', MIGRATION)
    assert created, "expected at least one created policy"
    for pol in created:
        assert pol.startswith("service_role_all_"), f"unexpected client policy created: {pol}"


def test_own_reply_engagement_specifically_fixed():
    # The externally-reported Security Advisor ERROR was on this table.
    sec = _section_for("threads_own_reply_engagement")
    assert "enable row level security" in sec
    assert "revoke all on table public.threads_own_reply_engagement from anon, authenticated" in sec
    assert "grant select, insert, update on table public.threads_own_reply_engagement to service_role" in sec
    assert 'create policy "service_role_all_threads_own_reply_engagement"' in sec


def test_migration_is_guarded_and_data_non_destructive():
    # every table guarded by to_regclass; no data reads/writes (no select/insert/update/delete
    # statements outside of grant statements); no drops of tables/indexes.
    body = re.sub(r"--[^\n]*", "", MIGRATION)
    assert "drop table" not in body.lower()
    assert "drop index" not in body.lower()
    # 'delete from' / 'insert into' / bare 'update <table> set' would be data mutations
    assert not re.search(r"\bdelete\s+from\b", body, re.I)
    assert not re.search(r"\binsert\s+into\b", body, re.I)
    assert not re.search(r"\bupdate\s+public\.", body, re.I)
