"""Regression tests for migration 018 DM opportunity least privilege."""

from pathlib import Path
import re

REPO = Path(__file__).resolve().parent.parent
SQL = (REPO / "migrations" / "018_dm_opportunity_security_reconcile.sql").read_text().lower()


def test_service_role_is_reduced_before_regrant():
    assert "revoke all on table public.threads_dm_opportunities from service_role" in SQL
    assert "grant select, insert, update on table public.threads_dm_opportunities to service_role" in SQL


def test_client_roles_remain_revoked():
    assert "revoke all on table public.threads_dm_opportunities from public" in SQL
    assert "revoke all on table public.threads_dm_opportunities from anon, authenticated" in SQL


def test_sequence_permissions_are_minimal():
    assert "revoke all on sequence public.threads_dm_opportunities_id_seq from service_role" in SQL
    assert "grant usage, select on sequence public.threads_dm_opportunities_id_seq to service_role" in SQL


def test_no_destructive_table_operations():
    body = re.sub(r"--[^\n]*", "", SQL)
    assert "drop table" not in body
    assert "truncate table" not in body
    assert "delete from" not in body
