from pathlib import Path


def test_existing_activity_table_has_non_destructive_multi_account_upgrade():
    sql = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "005_activity_multi_account_upgrade.sql"
    ).read_text().lower()

    assert "add column if not exists account_key text" in sql
    assert "where account_key is null" in sql
    assert "set account_key = 'legacy'" in sql
    assert "set not null" in sql
    assert "drop constraint if exists threads_activity_events_fallback_fingerprint_key" in sql
    assert "unique (account_key, fallback_fingerprint)" in sql
    assert "drop view if exists public.threads_follows_from_post_summary" in sql
    assert "group by account_key, matched_post_id" in sql
    assert "drop table" not in sql
