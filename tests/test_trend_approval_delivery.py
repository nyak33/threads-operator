"""Tests for Telegram-first approval delivery in Workflow A (trend engagement).

Covers the production bug where rows entered pending_approval before any
Telegram card was confirmed, and approval_sent_at was never stamped:

1.  Draft generated but Telegram send fails -> row stays discovered, no approval_sent_at
2.  Failed Telegram send -> does not timeout after 24h
3.  Successful Telegram send -> approval_sent_at populated
4.  Successful send -> pending_approval
5.  Repeat watchdog -> no duplicate Telegram card
6.  NULL approval_sent_at -> timeout worker ignores row
7.  non-null approval_sent_at older than 24h -> backlog
8.  Telegram message ref saved if supported
9.  existing Approve/Edit/Reject/Skip callbacks still work
10. legacy telegram-send path stamps approval_sent_at after confirmed delivery
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from threads_operator import content_generation, operator_cli, trend_engagement
from threads_operator.supabase_store import SupabaseStore


# --------------------------------------------------------------------------
# Fake PostgREST backend (mirrors test_two_engagement_workflows.py, with
# lt./is.null/not.is.null support for the approval-delivery CAS queries)
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
            elif raw == "not.is.null":
                if row.get(key) is None:
                    return False
        return True

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


@pytest.fixture()
def backend() -> FakePostgREST:
    return FakePostgREST()


@pytest.fixture()
def store(backend: FakePostgREST) -> SupabaseStore:
    transport = httpx.MockTransport(backend.handle)
    client = httpx.Client(transport=transport, base_url="http://test")
    return SupabaseStore("http://test", "service-key", client=client, account_key="syaqir")


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic verdicts/drafts without any LLM/network."""
    monkeypatch.setattr(
        content_generation,
        "generate_json",
        lambda **kwargs: {"score": 0.9, "relevant": True, "topic": "digital marketing",
                          "reason": "on-theme", "angle": "cerita pengalaman"},
    )
    monkeypatch.setattr(
        content_generation, "generate_text", lambda **kwargs: "draf asli yang padat",
    )


def _config(tmp_path) -> Any:
    from threads_operator.account_config import AccountConfig

    return AccountConfig(
        name="syaqir",
        home=tmp_path,
        env_path=tmp_path / "account.env",
        values={
            "SUPABASE_URL": "http://test",
            "SUPABASE_SERVICE_ROLE_KEY": "service-key",
            "THREADS_USER_ID": "123",
            "THREADS_ACCESS_TOKEN": "tok",
        },
    )


def _patched_store(config: Any, store: SupabaseStore):
    return patch.object(operator_cli, "_store", return_value=store)


def _run_draft(config: Any, store: SupabaseStore, tmp_path) -> tuple[int, dict[str, Any]]:
    with _patched_store(config, store), patch.object(
        trend_engagement, "load_persona", return_value="persona uji"
    ):
        return operator_cli._run_trendeng_draft(
            config, candidate_id=None, limit=5, threshold=0.6, dry_run=False,
        )


# --------------------------------------------------------------------------
# 1. Draft generated but Telegram send fails -> no approval_sent_at
# --------------------------------------------------------------------------

def test_telegram_failure_keeps_row_retryable(store, backend, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "79553451")
    row = _seed_candidate(store, backend, status="discovered")

    def _boom(**kwargs):
        raise RuntimeError("telegram api not ok: bot blocked")

    monkeypatch.setattr(operator_cli, "_send_trend_approval_card", _boom)
    config = _config(tmp_path)
    code, payload = _run_draft(config, store, tmp_path)

    assert code == 0
    assert payload["drafted_count"] == 0
    assert payload["error_count"] == 1
    assert "telegram send failed" in payload["errors"][0]["error"]

    fresh = store.get_trend_candidate(row["id"])
    # Claim-before-send: draft staged to `drafted`, claim released on failure.
    # Row stays retryable and approval_sent_at is never stamped.
    assert fresh["status"] == "drafted"
    assert fresh.get("approval_sent_at") is None


# --------------------------------------------------------------------------
# 2. Failed Telegram send -> does not timeout after 24h
# --------------------------------------------------------------------------

def test_failed_send_does_not_timeout_after_24h(store, backend, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "79553451")
    row = _seed_candidate(
        store, backend, status="discovered",
        updated_at=(datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
    )

    def _boom(**kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(operator_cli, "_send_trend_approval_card", _boom)
    config = _config(tmp_path)
    code, _ = _run_draft(config, store, tmp_path)
    assert code == 0

    # Row is NOT pending_approval, so even a 72h-old updated_at can't time it out.
    stale = store.find_stale_pending_approvals(older_than_hours=24)
    assert row["id"] not in [r["id"] for r in stale]
    fresh = store.get_trend_candidate(row["id"])
    assert fresh["status"] == "drafted"
    assert fresh.get("approval_sent_at") is None


# --------------------------------------------------------------------------
# 3 + 4. Successful Telegram send -> approval_sent_at populated, pending_approval
# --------------------------------------------------------------------------

def test_successful_send_promotes_and_stamps(store, backend, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "79553451")
    row = _seed_candidate(store, backend, status="discovered")
    sent: list[dict[str, Any]] = []

    def _ok_send(**kwargs):
        sent.append(kwargs)
        return "79553451:9001"

    monkeypatch.setattr(operator_cli, "_send_trend_approval_card", _ok_send)
    config = _config(tmp_path)
    code, payload = _run_draft(config, store, tmp_path)

    assert code == 0
    assert payload["drafted_count"] == 1
    assert payload["error_count"] == 0
    assert len(sent) == 1
    assert sent[0]["candidate_id"] == row["id"]

    fresh = store.get_trend_candidate(row["id"])
    assert fresh["status"] == "pending_approval"
    assert fresh["approval_sent_at"] is not None
    drafted = payload["drafted"][0]
    assert drafted["telegram_message_ref"] == "79553451:9001"


# --------------------------------------------------------------------------
# 5. Repeat watchdog -> no duplicate Telegram card
# --------------------------------------------------------------------------

def test_repeat_watchdog_never_resends_card(store, backend, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "79553451")
    row = _seed_candidate(store, backend, status="discovered")
    sent: list[dict[str, Any]] = []

    def _ok_send(**kwargs):
        sent.append(kwargs)
        return "79553451:9002"

    monkeypatch.setattr(operator_cli, "_send_trend_approval_card", _ok_send)
    config = _config(tmp_path)

    code1, payload1 = _run_draft(config, store, tmp_path)
    assert code1 == 0 and payload1["drafted_count"] == 1
    assert len(sent) == 1

    # Second watchdog run: row is already pending_approval with approval_sent_at
    # set, so the draft selector never picks it up again.
    code2, payload2 = _run_draft(config, store, tmp_path)
    assert code2 == 0
    assert payload2["drafted_count"] == 0
    assert len(sent) == 1

    # Store-level guard too: mark_trend_candidate_approval_sent is a no-op
    # once approval_sent_at is non-null.
    again = store.mark_trend_candidate_approval_sent(
        candidate_id=row["id"], approval_ref="79553451:9999",
    )
    assert again is None
    fresh = store.get_trend_candidate(row["id"])
    wa = (fresh.get("raw_metadata") or {}).get("workflow_a") or {}
    assert wa.get("approval_ref") == "79553451:9002"


# --------------------------------------------------------------------------
# 6 + 7. Timeout worker: NULL ignored, old non-null -> backlog
# --------------------------------------------------------------------------

def test_timeout_ignores_null_approval_sent_at(store, backend):
    row = _seed_candidate(
        store, backend, status="pending_approval",
        approval_sent_at=None,
        updated_at="2020-01-01T00:00:00+00:00",
    )
    stale = store.find_stale_pending_approvals(older_than_hours=24)
    assert row["id"] not in [r["id"] for r in stale]


def test_timeout_moves_old_approval_sent_at_to_backlog(store, backend):
    row = _seed_candidate(
        store, backend, status="pending_approval",
        approval_sent_at="2026-09-20T00:00:00+00:00",
        updated_at="2026-09-20T00:00:00+00:00",
    )
    stale = store.find_stale_pending_approvals(older_than_hours=24)
    assert row["id"] in [r["id"] for r in stale]
    moved = store.transition_trend_candidate_backlog(candidate_id=row["id"])
    assert moved is not None and moved["status"] == "backlog"


# --------------------------------------------------------------------------
# 8. Telegram message ref saved if supported
# --------------------------------------------------------------------------

def test_telegram_message_ref_persisted(store, backend, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "79553451")
    row = _seed_candidate(store, backend, status="discovered")
    monkeypatch.setattr(
        operator_cli, "_send_trend_approval_card", lambda **kw: "79553451:4242",
    )
    config = _config(tmp_path)
    code, payload = _run_draft(config, store, tmp_path)
    assert code == 0 and payload["drafted_count"] == 1

    fresh = store.get_trend_candidate(row["id"])
    wa = (fresh.get("raw_metadata") or {}).get("workflow_a") or {}
    assert wa.get("approval_ref") == "79553451:4242"
    assert wa.get("approval_message") == "79553451:4242"


# --------------------------------------------------------------------------
# 9. Approve/Edit/Reject/Skip callbacks still work
# --------------------------------------------------------------------------

def test_approve_reject_skip_callbacks_unchanged(store, backend, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "79553451")
    r1 = _seed_candidate(store, backend, status="pending_approval",
                         approval_sent_at=datetime.now(timezone.utc).isoformat(),
                         raw_metadata={"workflow_a": {"draft_text": "draf siap"}})
    r2 = _seed_candidate(store, backend, status="pending_approval",
                         approval_sent_at=datetime.now(timezone.utc).isoformat(),
                         raw_metadata={"workflow_a": {"draft_text": "draf siap"}})
    r3 = _seed_candidate(store, backend, status="pending_approval",
                         approval_sent_at=datetime.now(timezone.utc).isoformat(),
                         raw_metadata={"workflow_a": {"draft_text": "draf siap"}})

    config = _config(tmp_path)
    with _patched_store(config, store):
        code, payload = operator_cli._run_trendeng_approve(config, candidate_id=r1["id"])
        assert code == 0 and payload["ok"]

        code, payload = operator_cli._run_trendeng_edit(
            config, candidate_id=r2["id"], text="draf edit",
        )
        assert code == 0 and payload["ok"]

        code, payload = operator_cli._run_trendeng_skip(config, candidate_id=r3["id"])
        assert code == 0 and payload["ok"]

    assert store.get_trend_candidate(r1["id"])["status"] in {"approved", "queued"}
    edited = store.get_trend_candidate(r2["id"])
    wa = (edited.get("raw_metadata") or {}).get("workflow_a") or {}
    assert wa.get("draft_text") == "draf edit" or edited.get("status") == "pending_approval"
    assert store.get_trend_candidate(r3["id"])["status"] == "skipped"


# --------------------------------------------------------------------------
# 10. Legacy telegram-send path: stamps approval_sent_at only after delivery,
#     and refuses to duplicate-send a row that already has one.
# --------------------------------------------------------------------------

def test_legacy_telegram_send_stamps_after_delivery(store, backend, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "79553451")
    row = _seed_candidate(
        store, backend, status="pending_approval", approval_sent_at=None,
        raw_metadata={"workflow_a": {"draft_text": "draf legacy"}},
    )
    calls: list[dict[str, Any]] = []

    def _ok_send(**kwargs):
        calls.append(kwargs)
        return "79553451:7777"

    monkeypatch.setattr(operator_cli, "_send_trend_approval_card", _ok_send)
    config = _config(tmp_path)
    with _patched_store(config, store):
        code, payload = operator_cli._run_trendeng_telegram_send(
            config, candidate_id=row["id"], card=None,
        )
    assert code == 0 and payload["ok"]
    assert payload["approval_sent_at"] is not None
    assert payload["telegram_message_ref"] == "79553451:7777"
    assert len(calls) == 1

    fresh = store.get_trend_candidate(row["id"])
    assert fresh["approval_sent_at"] is not None
    wa = (fresh.get("raw_metadata") or {}).get("workflow_a") or {}
    assert wa.get("approval_ref") == "79553451:7777"

    # Repeat send must skip (duplicate prevention for legacy path too).
    with _patched_store(config, store):
        code, payload = operator_cli._run_trendeng_telegram_send(
            config, candidate_id=row["id"], card=None,
        )
    assert code == 0 and payload["ok"] and payload.get("skipped")
    assert len(calls) == 1


def test_legacy_telegram_send_concurrent_claim_skips_send(store, backend, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "79553451")
    row = _seed_candidate(
        store, backend, status="pending_approval", approval_sent_at=None,
        raw_metadata={"workflow_a": {"draft_text": "draf legacy"}},
    )
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        operator_cli,
        "_send_trend_approval_card",
        lambda **kw: calls.append(kw) or "79553451:8888",
    )

    # Simulate another worker winning the claim BEFORE we even send: our CAS
    # returns None and the card must never leave the process.
    store.mark_trend_candidate_approval_sent = lambda **kw: None  # type: ignore[assignment]
    config = _config(tmp_path)
    with _patched_store(config, store):
        code, payload = operator_cli._run_trendeng_telegram_send(
            config, candidate_id=row["id"], card=None,
        )
    assert code == 0 and payload["ok"] and payload.get("skipped")
    assert calls == []


def test_legacy_telegram_send_failure_keeps_retryable(store, backend, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "79553451")
    row = _seed_candidate(
        store, backend, status="pending_approval", approval_sent_at=None,
        raw_metadata={"workflow_a": {"draft_text": "draf legacy"}},
    )

    def _boom(**kwargs):
        raise RuntimeError("telegram api not ok")

    monkeypatch.setattr(operator_cli, "_send_trend_approval_card", _boom)
    config = _config(tmp_path)
    with _patched_store(config, store):
        code, payload = operator_cli._run_trendeng_telegram_send(
            config, candidate_id=row["id"], card=None,
        )
    assert code == 1 and not payload["ok"]
    fresh = store.get_trend_candidate(row["id"])
    assert fresh["status"] == "pending_approval"
    assert fresh.get("approval_sent_at") is None


# --------------------------------------------------------------------------
# 11. No Telegram creds: draft persists as `drafted`, timer never starts
# --------------------------------------------------------------------------

def test_no_telegram_creds_stays_drafted_not_pending(store, backend, tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    row = _seed_candidate(store, backend, status="discovered")
    config = _config(tmp_path)
    code, payload = _run_draft(config, store, tmp_path)

    assert code == 0
    assert payload["telegram_sent"] is False
    fresh = store.get_trend_candidate(row["id"])
    assert fresh["status"] == "drafted"
    assert fresh.get("approval_sent_at") is None

    # And it must never time out from this state either.
    stale = store.find_stale_pending_approvals(older_than_hours=24)
    assert row["id"] not in [r["id"] for r in stale]
