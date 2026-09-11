from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (ROOT / "migrations" / "001_threads_insights.sql").read_text().lower()


def test_insights_tables_enable_row_level_security():
    for table in (
        "threads_account_snapshots",
        "threads_post_snapshots",
        "threads_daily_rollups",
    ):
        assert f"alter table {table} enable row level security;" in MIGRATION
