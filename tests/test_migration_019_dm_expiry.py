"""Regression tests for migration 019 DM opportunity expiry deadline."""

from pathlib import Path
import re

REPO = Path(__file__).resolve().parent.parent
SQL = (REPO / "migrations" / "019_dm_opportunity_expires_at.sql").read_text().lower()


def test_expires_at_column_is_added_idempotently():
    assert "add column if not exists expires_at timestamptz" in SQL


def test_expiry_index_is_account_scoped_and_partial():
    assert "threads_dm_opportunities_expiry_idx" in SQL
    assert "(account_key, expires_at)" in SQL
    assert "where status in" in SQL


def test_migration_is_non_destructive():
    body = re.sub(r"--[^\n]*", "", SQL)
    assert "drop table" not in body
    assert "delete from" not in body
    assert "truncate" not in body
