from pathlib import Path


def test_publish_recovery_migration_adds_retry_states_and_metadata():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "007_publish_recovery.sql"
    )
    assert path.exists(), "publish recovery migration must exist"

    sql = path.read_text().lower()
    assert "'retrying'" in sql
    assert "'needs_attention'" in sql
    assert "attempt_count" in sql
    assert "next_retry_at" in sql
    assert "retry_deadline_at" in sql
    assert "last_error_meta" in sql
