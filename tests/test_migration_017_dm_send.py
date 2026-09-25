"""Regression tests for migration 017 DM send status constraint.

Migration 017 replaces the status CHECK from migration 015 to add
`send_uncertain`. It must preserve every pre-existing lifecycle state,
including `failed` and `expired`, or legitimate Task 2C/2D transitions
will fail at the database layer.
"""

from pathlib import Path
import re

REPO = Path(__file__).resolve().parent.parent
SQL = (REPO / "migrations" / "017_dm_send.sql").read_text()


def _status_values() -> set[str]:
    match = re.search(
        r"add constraint\s+threads_dm_opportunities_status_check\s+"
        r"check\s*\(status\s+in\s*\((.*?)\)\)",
        SQL,
        re.I | re.S,
    )
    assert match, "migration 017 status constraint not found"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_migration_017_preserves_full_dm_lifecycle():
    assert _status_values() == {
        "detected",
        "drafted",
        "awaiting_approval",
        "approved",
        "sending",
        "sent",
        "rejected",
        "failed",
        "expired",
        "cancelled",
        "send_uncertain",
    }


def test_migration_017_is_non_destructive_to_tables():
    body = re.sub(r"--[^\n]*", "", SQL)
    assert "drop table" not in body.lower()
