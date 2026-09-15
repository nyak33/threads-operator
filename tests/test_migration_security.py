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


def test_forward_security_migration_revokes_client_roles_from_operator_tables():
    path = ROOT / "migrations" / "006_security_hardening.sql"
    assert path.exists(), "forward security migration 006 is required"
    sql = path.read_text().lower()

    for table in (
        "threads_account_insights_snapshots",
        "threads_post_insights_snapshots",
        "threads_daily_rollups",
    ):
        assert f"revoke all on table public.{table} from anon, authenticated;" in sql
        assert f"grant" in sql and f"public.{table}" in sql and "service_role" in sql
