from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_fresh_activity_schema_does_not_require_external_threads_posts_table():
    sql = (ROOT / "migrations" / "003_activity_follow_events.sql").read_text().lower()
    assert "references public.threads_posts" not in sql
    assert "matched_post_id text" in sql


def test_existing_activity_upgrade_removes_legacy_matched_post_foreign_key():
    sql = (ROOT / "migrations" / "005_activity_multi_account_upgrade.sql").read_text().lower()
    assert "drop constraint if exists threads_activity_events_matched_post_id_fkey" in sql
