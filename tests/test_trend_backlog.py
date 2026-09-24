"""Tests for the 24-hour approval timeout + content backlog (Workflow A only).

Covers every case the user asked for:
1.  pending_approval <24h stays active
2.  pending_approval >24h -> backlog
3.  approved item never times out
4.  rejected/skipped/queued item never times out
5.  timeout worker is idempotent
6.  old approval callback cannot enqueue backlog item
7.  /content-backlog only returns correct account
8.  Use -> exactly one queue row
9.  duplicate Use -> no second queue row
10. Edit persists
11. Refresh uses dynamic provider/persona
12. Discard removes item from active backlog
13. topic preserved backlog -> publish queue
14. existing trend and own-reply regression tests still pass (run via suite)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from threads_operator import content_generation, trend_engagement
from threads_operator.account_config import AccountConfig
from threads_operator.supabase_store import SupabaseStore, TREND_STATUSES


# --------------------------------------------------------------------------
# Fake PostgREST backend (mirrors the one in test_two_engagement_workflows.py)
# --------------------------------------------------------------------------

class FakePostgREST:
    """Minimal in-memory PostgREST for the tables these workflows touch."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {
            "threads_trend_candidates": [],
            "threads_own_reply_engagement": [],
            "threads_publish_queue": [],
        }
        self._ids: dict[str, int] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        table = request.url.path.rsplit("/", 1)[-1]
        rows = self.tables.setdefault(table, [])
        params = dict(request.url.params)
        if request.method == "GET":
            return self._select(rows, params)
        if request.method == "POST":
            body = json.loads(request.content.decode() or "{}")
            return self._insert(table, rows, body)
        if request.method == "PATCH":
            body = json.loads(request.content.decode() or "{}")
            return self._update(rows, params, body)
        return httpx.Response(405, json={"error": "method"})

    def _matches(self, row: dict[str, Any], params: dict[str, str]) -> bool:
        for key, raw in params.items():
            if key in {"select", "order", "limit"}:
                continue
            if raw.startswith("eq."):
                expected: Any = raw[3:]
                actual = row.get(key)
                if str(actual) != expected and actual != self._coerce(expected):
                    return False
            elif raw.startswith("in.("):
                options = raw[4:-1].split(",")
                if str(row.get(key)) not in options:
                    return False
            elif raw.startswith("lt."):
                cutoff = raw[3:]
                actual = row.get(key)
                if actual is None:
                    return False
                actual_dt = datetime.fromisoformat(str(actual).replace("Z", "+00:00"))
                cutoff_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
                if actual_dt >= cutoff_dt:
                    return False
            elif raw.startswith("is.null"):
                if row.get(key) is not None:
                    return False
            elif key == "or":
                # Handle simple or-filters used by find_stale_pending_approvals
                if not self._matches_or(row, raw):
                    return False
        return True

    @staticmethod
    def _matches_or(row: dict[str, Any], raw: str) -> bool:
        """Support the specific or-filter used by find_stale_pending_approvals.

        Shape: (approval_sent_at.lt.<cutoff>,and(approval_sent_at.is.null,updated_at.lt.<cutoff>))
        """
        # approval_sent_at.lt.cutoff
        cutoff_start = raw.find("lt.") + 3
        cutoff_end = raw.find(",", cutoff_start)
        if cutoff_end == -1:
            cutoff_end = raw.find(")", cutoff_start)
        cutoff = raw[cutoff_start:cutoff_end]
        cutoff_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        sent = row.get("approval_sent_at")
        if sent is not None:
            sent_dt = datetime.fromisoformat(str(sent).replace("Z", "+00:00"))
            if sent_dt < cutoff_dt:
                return True
        # approval_sent_at is null AND updated_at.lt.cutoff
        if sent is None:
            updated = row.get("updated_at")
            if updated is not None:
                updated_dt = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
                if updated_dt < cutoff_dt:
                    return True
        return False

    @staticmethod
    def _coerce(value: str) -> Any:
        try:
            return int(value)
        except ValueError:
            return value

    def _select(self, rows: list[dict[str, Any]], params: dict[str, str]) -> httpx.Response:
        matched = [dict(r) for r in rows if self._matches(r, params)]
        limit = params.get("limit")
        if limit and limit.isdigit():
            matched = matched[: int(limit)]
        return httpx.Response(200, json=matched)

    def _insert(self, table: str, rows: list[dict[str, Any]], body: dict[str, Any]) -> httpx.Response:
        next_id = self._ids.get(table, 0) + 1
        self._ids[table] = next_id
        row = {"id": next_id, **body}
        if table == "threads_own_reply_engagement":
            for existing in rows:
                if (
                    existing.get("account_key") == row.get("account_key")
                    and existing.get("reply_id") == row.get("reply_id")
                ):
                    return httpx.Response(409, json={"code": "23505"})
        rows.append(row)
        return httpx.Response(201, json=[dict(row)])

    def _update(
        self, rows: list[dict[str, Any]], params: dict[str, str], body: dict[str, Any]
    ) -> httpx.Response:
        updated: list[dict[str, Any]] = []
        for row in rows:
            if self._matches(row, params):
                row.update(body)
                updated.append(dict(row))
        return httpx.Response(200, json=updated)


@pytest.fixture()
def backend() -> FakePostgREST:
    return FakePostgREST()


@pytest.fixture()
def store(backend: FakePostgREST) -> SupabaseStore:
    transport = httpx.MockTransport(backend.handle)
    client = httpx.Client(transport=transport, base_url="http://test")
    return SupabaseStore("http://test", "service-key", client=client, account_key="syaqir")


def _seed_candidate(store: SupabaseStore, backend: FakePostgREST, **over: Any) -> dict[str, Any]:
    row = {
        "id": backend._ids.get("threads_trend_candidates", 0) + 1,
        "target_account_id": "syaqir",
        "source_platform": "threads",
        "source_post_id": None,
        "source_username": "najmilatifnasrudin",
        "source_permalink": "https://www.threads.com/@najmilatifnasrudin/post/ABC123",
        "source_text": "Aku dah 10 tahun handle marketing SME Malaysia...",
        "status": "discovered",
        "raw_metadata": {"candidate_roles": ["external_trend"]},
        "topic": None,
        "trend_score": None,
        "adaptation_angle": None,
        "why_it_works": None,
        "used_in_queue_id": None,
        "approval_sent_at": None,
        "timed_out_at": None,
        "updated_at": "2026-09-22T00:00:00+00:00",
    }
    row.update(over)
    backend._ids["threads_trend_candidates"] = row["id"]
    backend.tables["threads_trend_candidates"].append(row)
    return row


def _seed_backlog_candidate(store: SupabaseStore, backend: FakePostgREST, **over: Any) -> dict[str, Any]:
    """Seed a candidate that is already in backlog state."""
    row = _seed_candidate(
        store, backend, status="backlog", timed_out_at="2026-09-21T00:00:00+00:00",
        topic="digital marketing",
    )
    raw = dict(row.get("raw_metadata") or {})
    raw["workflow_a"] = {
        "draft_text": "draf backlog",
        "topic": "digital marketing",
        "reason": "sesuai untuk account",
        "angle": "cerita pengalaman sendiri",
    }
    row["raw_metadata"] = raw
    row.update(over)
    return row


# --------------------------------------------------------------------------
# 1. pending_approval <24h stays active
# --------------------------------------------------------------------------

def test_pending_approval_under_24h_stays_active(store, backend):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=2)).isoformat()
    old = (now - timedelta(hours=25)).isoformat()
    # seed one recent pending row (should stay) and one stale row (so the filter returns something)
    recent_row = _seed_candidate(
        store, backend, status="pending_approval",
        approval_sent_at=recent, updated_at=recent,
    )
    stale_row = _seed_candidate(
        store, backend, status="pending_approval",
        approval_sent_at=old, updated_at=old,
    )
    stale = store.find_stale_pending_approvals(older_than_hours=24)
    stale_ids = {r["id"] for r in stale}
    assert recent_row["id"] not in stale_ids
    assert stale_row["id"] in stale_ids


# --------------------------------------------------------------------------
# 2. pending_approval >24h -> backlog
# --------------------------------------------------------------------------

def test_pending_approval_over_24h_moves_to_backlog(store, backend):
    row = _seed_candidate(
        store, backend, status="pending_approval",
        approval_sent_at="2026-09-20T00:00:00+00:00",
    )
    stale = store.find_stale_pending_approvals(older_than_hours=24)
    assert row["id"] in [r["id"] for r in stale]
    moved = store.transition_trend_candidate_backlog(candidate_id=row["id"])
    assert moved is not None
    assert moved["status"] == "backlog"
    assert moved["timed_out_at"] is not None


def test_pending_approval_null_approval_sent_at_never_times_out(store, backend):
    """STRICT RULE: approval_sent_at IS NULL must NEVER time out into backlog.

    The old fallback (approval_sent_at NULL -> updated_at) moved rows whose
    Telegram card was never delivered. That fallback is removed; even an
    ancient updated_at must not make the row stale.
    """
    row = _seed_candidate(
        store, backend, status="pending_approval",
        approval_sent_at=None,
        updated_at="2026-09-20T00:00:00+00:00",
    )
    stale = store.find_stale_pending_approvals(older_than_hours=24)
    assert row["id"] not in [r["id"] for r in stale]


# --------------------------------------------------------------------------
# 3. approved item never times out
# --------------------------------------------------------------------------

def test_approved_item_never_times_out(store, backend):
    row = _seed_candidate(
        store, backend, status="approved",
        approval_sent_at="2026-09-20T00:00:00+00:00",
    )
    stale = store.find_stale_pending_approvals(older_than_hours=24)
    assert row["id"] not in [r["id"] for r in stale]
    moved = store.transition_trend_candidate_backlog(candidate_id=row["id"])
    assert moved is None


# --------------------------------------------------------------------------
# 4. rejected/skipped/queued item never times out
# --------------------------------------------------------------------------

@pytest.mark.parametrize("terminal_status", ["rejected", "skipped", "queued"])
def test_terminal_status_never_times_out(store, backend, terminal_status):
    row = _seed_candidate(
        store, backend, status=terminal_status,
        approval_sent_at="2026-09-20T00:00:00+00:00",
    )
    stale = store.find_stale_pending_approvals(older_than_hours=24)
    assert row["id"] not in [r["id"] for r in stale]
    moved = store.transition_trend_candidate_backlog(candidate_id=row["id"])
    assert moved is None


# --------------------------------------------------------------------------
# 5. timeout worker is idempotent
# --------------------------------------------------------------------------

def test_timeout_worker_is_idempotent(store, backend):
    row = _seed_candidate(
        store, backend, status="pending_approval",
        approval_sent_at="2026-09-20T00:00:00+00:00",
    )
    first = store.transition_trend_candidate_backlog(candidate_id=row["id"])
    assert first is not None and first["status"] == "backlog"
    # second call must be a safe no-op
    second = store.transition_trend_candidate_backlog(candidate_id=row["id"])
    assert second is None
    # still only one backlog row
    rows = [r for r in backend.tables["threads_trend_candidates"] if r["id"] == row["id"]]
    assert len(rows) == 1
    assert rows[0]["status"] == "backlog"


# --------------------------------------------------------------------------
# 6. old approval callback cannot enqueue backlog item
# --------------------------------------------------------------------------

def test_old_approve_callback_cannot_enqueue_backlog_item(store, backend):
    row = _seed_candidate(
        store, backend, status="backlog",
        timed_out_at="2026-09-21T00:00:00+00:00",
        raw_metadata={"workflow_a": {"draft_text": "draf", "topic": "digital marketing"}},
    )
    # Approve requires pending_approval; backlog must fail
    moved = store.transition_trend_candidate(
        candidate_id=row["id"], from_status="pending_approval", to_status="approved"
    )
    assert moved is None
    current = [r for r in backend.tables["threads_trend_candidates"] if r["id"] == row["id"]][0]
    assert current["status"] == "backlog"
    assert len(backend.tables["threads_publish_queue"]) == 0


# --------------------------------------------------------------------------
# 7. /content-backlog only returns correct account
# --------------------------------------------------------------------------

def test_backlog_list_is_account_scoped(store, backend):
    _seed_backlog_candidate(store, backend)
    _seed_candidate(
        store, backend, status="backlog", target_account_id="victim",
        timed_out_at="2026-09-21T00:00:00+00:00",
    )
    rows = store.list_trend_backlog()
    assert len(rows) == 1
    assert rows[0]["target_account_id"] == "syaqir"


# --------------------------------------------------------------------------
# 8. Use -> exactly one queue row
# --------------------------------------------------------------------------

def test_backlog_use_enqueues_exactly_one_queue_row(store, backend):
    row = _seed_backlog_candidate(store, backend)
    # emulate the CLI use: CAS -> enqueue -> mark queued
    moved = store.transition_trend_candidate(
        candidate_id=row["id"], from_status="backlog", to_status="approved"
    )
    assert moved is not None
    q = store.enqueue_draft(
        "threads_publish_queue", "draf backlog", reply_texts=[], topic="digital marketing"
    )
    store.update_trend_candidate_workflow_a(
        candidate_id=row["id"],
        fields={"status": "queued", "used_in_queue_id": int(q["id"])},
    )
    assert len(backend.tables["threads_publish_queue"]) == 1


# --------------------------------------------------------------------------
# 9. duplicate Use -> no second queue row
# --------------------------------------------------------------------------

def test_duplicate_backlog_use_does_not_create_second_queue_row(store, backend):
    row = _seed_backlog_candidate(store, backend)
    moved = store.transition_trend_candidate(
        candidate_id=row["id"], from_status="backlog", to_status="approved"
    )
    assert moved is not None
    q1 = store.enqueue_draft(
        "threads_publish_queue", "draf backlog", reply_texts=[], topic="digital marketing"
    )
    store.update_trend_candidate_workflow_a(
        candidate_id=row["id"],
        fields={"status": "queued", "used_in_queue_id": int(q1["id"])},
    )
    # second use attempt: used_in_queue_id is set -> already_queued path
    current = store.get_trend_candidate(row["id"])
    assert current is not None and current["used_in_queue_id"] == q1["id"]
    # A second CAS from backlog must also fail
    again = store.transition_trend_candidate(
        candidate_id=row["id"], from_status="backlog", to_status="approved"
    )
    assert again is None
    assert len(backend.tables["threads_publish_queue"]) == 1


# --------------------------------------------------------------------------
# 10. Edit persists
# --------------------------------------------------------------------------

def test_backlog_edit_persists(store, backend):
    row = _seed_backlog_candidate(store, backend)
    updated = store.update_trend_candidate_workflow_a(
        candidate_id=row["id"],
        fields={"raw_metadata": {"workflow_a": {"draft_text": "edited text", "edited": True}}},
    )
    assert updated is not None
    wa = updated["raw_metadata"]["workflow_a"]
    assert wa["draft_text"] == "edited text"
    assert wa["edited"] is True
    # remains in backlog
    assert updated["status"] == "backlog"


# --------------------------------------------------------------------------
# 11. Refresh uses dynamic provider/persona
# --------------------------------------------------------------------------

def test_refresh_uses_dynamic_provider_and_persona(store, backend, tmp_path):
    row = _seed_backlog_candidate(store, backend)
    persona_file = tmp_path / "syaqir.md"
    persona_file.write_text("persona text", encoding="utf-8")
    persona = trend_engagement.load_persona(tmp_path, "syaqir")
    assert persona == "persona text"

    with patch.object(
        content_generation, "generate_text", return_value="refreshed draft"
    ) as gen:
        draft = trend_engagement.refresh_original_draft(
            row, persona_text=persona
        )
    assert draft == "refreshed draft"
    assert gen.call_count == 1
    # verify the persona is actually sent in the system prompt
    call_kwargs = gen.call_args[1]
    assert "persona text" in call_kwargs["system_prompt"]

    # apply refresh via store update
    raw = dict(row.get("raw_metadata") or {})
    wa = dict(raw.get("workflow_a") or {})
    wa["draft_text"] = draft
    wa["refreshed_at"] = "2026-09-22T12:00:00+00:00"
    raw["workflow_a"] = wa
    updated = store.update_trend_candidate_workflow_a(
        candidate_id=row["id"], fields={"raw_metadata": raw}
    )
    assert updated is not None
    assert updated["raw_metadata"]["workflow_a"]["draft_text"] == "refreshed draft"
    assert updated["status"] == "backlog"


# --------------------------------------------------------------------------
# 12. Discard removes item from active backlog
# --------------------------------------------------------------------------

def test_discard_removes_from_active_backlog(store, backend):
    row = _seed_backlog_candidate(store, backend)
    moved = store.transition_trend_candidate(
        candidate_id=row["id"], from_status="backlog", to_status="discarded"
    )
    assert moved is not None and moved["status"] == "discarded"
    rows = store.list_trend_backlog()
    assert row["id"] not in [r["id"] for r in rows]


# --------------------------------------------------------------------------
# 13. topic preserved backlog -> publish queue
# --------------------------------------------------------------------------

def test_topic_preserved_backlog_to_publish_queue(store, backend):
    row = _seed_backlog_candidate(store, backend)
    # set the top-level topic column (migration 010 payload shape)
    row["topic"] = "digital marketing"
    moved = store.transition_trend_candidate(
        candidate_id=row["id"], from_status="backlog", to_status="approved"
    )
    assert moved is not None
    q = store.enqueue_draft(
        "threads_publish_queue", "draf backlog", reply_texts=[], topic="digital marketing"
    )
    store.update_trend_candidate_workflow_a(
        candidate_id=row["id"],
        fields={"status": "queued", "used_in_queue_id": int(q["id"])},
    )
    queue_row = backend.tables["threads_publish_queue"][0]
    assert queue_row["topic"] == "digital marketing"
    current = store.get_trend_candidate(row["id"])
    assert current["topic"] == "digital marketing"


# --------------------------------------------------------------------------
# Migration alignment
# --------------------------------------------------------------------------

def test_backlog_and_discarded_are_valid_trend_statuses():
    assert "backlog" in TREND_STATUSES
    assert "discarded" in TREND_STATUSES


# --------------------------------------------------------------------------
# CLI-level smoke tests (integration of the new subcommands)
# --------------------------------------------------------------------------

def test_cli_timeout_dry_run(store, backend):
    from threads_operator.operator_cli import _run_trendeng_timeout

    _seed_candidate(
        store, backend, status="pending_approval",
        approval_sent_at="2026-09-20T00:00:00+00:00",
    )
    config, old_store = _make_config(store)
    try:
        code, payload = _run_trendeng_timeout(config, older_than_hours=24, dry_run=True)
    finally:
        _restore_store(old_store)
    assert code == 0
    assert payload["ok"] is True
    assert payload["stale_count"] == 1
    assert payload["dry_run"] is True


def test_cli_backlog_list(store, backend):
    from threads_operator.operator_cli import _run_trendeng_backlog

    _seed_backlog_candidate(store, backend)
    config, old_store = _make_config(store)
    try:
        code, payload = _run_trendeng_backlog(config, limit=10)
    finally:
        _restore_store(old_store)
    assert code == 0
    assert payload["ok"] is True
    assert payload["count"] == 1
    item = payload["items"][0]
    assert item["topic"] == "digital marketing"
    assert item["draft_text"] == "draf backlog"


def test_cli_use_enqueues(store, backend):
    from threads_operator.operator_cli import _run_trendeng_use

    row = _seed_backlog_candidate(store, backend)
    config, old_store = _make_config(store)
    try:
        code, payload = _run_trendeng_use(config, candidate_id=row["id"])
    finally:
        _restore_store(old_store)
    assert code == 0
    assert payload["ok"] is True
    assert payload["status"] == "queued"
    assert payload["topic"] == "digital marketing"
    assert len(backend.tables["threads_publish_queue"]) == 1


def test_cli_use_idempotent(store, backend):
    from threads_operator.operator_cli import _run_trendeng_use

    row = _seed_backlog_candidate(store, backend)
    config, old_store = _make_config(store)
    try:
        code1, payload1 = _run_trendeng_use(config, candidate_id=row["id"])
        assert code1 == 0
        code2, payload2 = _run_trendeng_use(config, candidate_id=row["id"])
    finally:
        _restore_store(old_store)
    assert code2 == 0
    assert payload2.get("already_queued") is True
    assert len(backend.tables["threads_publish_queue"]) == 1


def test_cli_discard(store, backend):
    from threads_operator.operator_cli import _run_trendeng_discard

    row = _seed_backlog_candidate(store, backend)
    config, old_store = _make_config(store)
    try:
        code, payload = _run_trendeng_discard(config, candidate_id=row["id"])
    finally:
        _restore_store(old_store)
    assert code == 0
    assert payload["ok"] is True
    assert payload["status"] == "discarded"
    rows = store.list_trend_backlog()
    assert row["id"] not in [r["id"] for r in rows]


def test_cli_refresh(store, backend, tmp_path):
    from threads_operator.operator_cli import _run_trendeng_refresh

    row = _seed_backlog_candidate(store, backend)
    persona_file = tmp_path / "syaqir.md"
    persona_file.write_text("persona", encoding="utf-8")

    with patch.object(
        content_generation, "generate_text", return_value="refreshed draft"
    ):
        config, old_store = _make_config(store)
        # patch personas root
        import threads_operator.operator_cli as cli_mod
        old_root = cli_mod._personas_root
        cli_mod._personas_root = lambda: tmp_path
        try:
            code, payload = _run_trendeng_refresh(config, candidate_id=row["id"])
        finally:
            cli_mod._personas_root = old_root
            _restore_store(old_store)
    assert code == 0
    assert payload["ok"] is True
    assert payload["draft_text"] == "refreshed draft"


def test_cli_edit_persists(store, backend):
    from threads_operator.operator_cli import _run_trendeng_edit

    row = _seed_backlog_candidate(store, backend)
    config, old_store = _make_config(store)
    try:
        code, payload = _run_trendeng_edit(config, candidate_id=row["id"], text="edited text")
    finally:
        _restore_store(old_store)
    assert code == 0
    assert payload["ok"] is True
    assert payload["draft_text"] == "edited text"
    current = store.get_trend_candidate(row["id"])
    assert current["raw_metadata"]["workflow_a"]["draft_text"] == "edited text"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

class _FakeConfig(AccountConfig):
    def __init__(self):
        super().__init__(
            name="syaqir",
            home=Path("/tmp/fake-home"),
            env_path=Path("/tmp/fake-home/accounts/syaqir.env"),
            values={},
        )

    def get(self, key, default=None):
        return default

    def get_bool(self, key):
        return False

    def require(self, key):
        return "dummy"

def _make_config(store):
    """Build a fake AccountConfig that returns the test store via patched _store()."""
    config = _FakeConfig()
    # patch the module-level _store helper to always return our fake store
    import threads_operator.operator_cli as cli_mod
    old_store = cli_mod._store
    cli_mod._store = lambda cfg: store
    return config, old_store

def _restore_store(old_store):
    import threads_operator.operator_cli as cli_mod
    cli_mod._store = old_store
