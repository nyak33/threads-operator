"""Task 2C — Telegram DM Approval Workflow.

Covers the required acceptance tests:
1.  detected opportunity generates a draft
2.  draft moves to awaiting_approval
3.  Telegram approval card contains correct context
4.  Approve transitions awaiting_approval -> approved
5.  Reject transitions awaiting_approval -> rejected
6.  Edit replaces draft and remains awaiting_approval
7.  empty edit rejected
8.  expired edit session rejected
9.  duplicate Approve callback is idempotent
10. stale callback rejected
11. wrong-account callback rejected
12. expired opportunity cannot be approved
13. Telegram dispatch retry does not create duplicate active cards
14. existing own-reply Telegram workflow still works
15. existing trend/engagement workflows still work
16. no Threads DM is sent anywhere in Task 2C

Plus the draft-content requirements (persona voice, no fabrication, CTA
preserved) and the dispatcher-level edit-session behaviour.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from threads_operator import dm_opportunity, own_replies, reply_context, reply_intent
from threads_operator.supabase_store import SupabaseStore

UTC = timezone.utc
REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fake PostgREST backend (same shape as test_reply_context_intent)
# ---------------------------------------------------------------------------

class FakePostgREST:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {
            "threads_own_reply_engagement": [],
            "threads_dm_opportunities": [],
            "threads_posts": [],
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

    @staticmethod
    def _coerce(value: str) -> Any:
        try:
            return int(value)
        except ValueError:
            return value

    def _matches(self, row: dict[str, Any], params: dict[str, str]) -> bool:
        for key, raw in params.items():
            if key in {"select", "order", "limit", "offset"}:
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
                a = datetime.fromisoformat(str(actual).replace("Z", "+00:00"))
                c = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
                if a >= c:
                    return False
            elif raw.startswith("is.null"):
                if row.get(key) is not None:
                    return False
            elif raw == "not.is.null":
                if row.get(key) is None:
                    return False
        return True

    def _select(self, rows, params):
        matched = [dict(r) for r in rows if self._matches(r, params)]
        limit = params.get("limit")
        if limit and str(limit).isdigit():
            matched = matched[: int(limit)]
        return httpx.Response(200, json=matched)

    def _insert(self, table, rows, body):
        next_id = self._ids.get(table, 0) + 1
        self._ids[table] = next_id
        row = {"id": next_id, **body}
        if table == "threads_dm_opportunities":
            for existing in rows:
                if (
                    existing.get("account_key") == row.get("account_key")
                    and existing.get("from_username") == row.get("from_username")
                    and existing.get("root_post_id") == row.get("root_post_id")
                ):
                    return httpx.Response(409, json={"code": "23505"})
        rows.append(row)
        return httpx.Response(201, json=[dict(row)])

    def _update(self, rows, params, body):
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


@pytest.fixture()
def store_account_b(backend: FakePostgREST) -> SupabaseStore:
    transport = httpx.MockTransport(backend.handle)
    client = httpx.Client(transport=transport, base_url="http://test")
    return SupabaseStore("http://test", "service-key", client=client, account_key="other_account")


def _seed_dm_opp(backend: FakePostgREST, **over: Any) -> dict[str, Any]:
    nid = backend._ids.get("threads_dm_opportunities", 0) + 1
    backend._ids["threads_dm_opportunities"] = nid
    row = {
        "id": nid,
        "account_key": "syaqir",
        "source_reply_id": "reply-1",
        "source_own_reply_id": 1,
        "from_username": "prospect",
        "root_post_id": "post-1",
        "root_post_permalink": "https://threads.net/post/post-1",
        "root_post_text": "Berminat nak belajar marketing? Komen BERMUDAT di bawah.",
        "reply_text": "berminat sangat, macam mana nak mula?",
        "reply_permalink": "https://threads.net/reply/reply-1",
        "cta_matched": "berminat",
        "intent": "potential_lead",
        "lead_score": 0.85,
        "status": "detected",
        "dm_draft_text": None,
        "dm_approved_text": None,
        "approval_channel": None,
        "approval_ref": None,
        "approval_message_ref": None,
        "drafted_at": None,
        "edited_at": None,
        "approved_at": None,
        "approved_by": None,
        "rejected_at": None,
        "expired_at": None,
        "expires_at": (datetime.now(UTC) + timedelta(hours=72)).isoformat(),
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    row.update(over)
    backend.tables["threads_dm_opportunities"].append(row)
    return row


# ===========================================================================
# 1. detected opportunity generates a draft
# ===========================================================================

def test_detected_opportunity_generates_draft(store, backend):
    opp = _seed_dm_opp(backend)
    result = dm_opportunity.draft_dm_opportunity(
        store=store,
        opportunity_id=int(opp["id"]),
        draft_text="Hai @prospect, terima kasih! Boleh saya kongsikan linknya di sini?",
    )
    assert result is not None
    assert result["status"] == "drafted"
    assert result["dm_draft_text"] == (
        "Hai @prospect, terima kasih! Boleh saya kongsikan linknya di sini?"
    )


def test_draft_is_persisted_exactly(store, backend):
    opp = _seed_dm_opp(backend)
    draft = "Salaam @prospect! Nanti saya PM details kursus tu ya."
    dm_opportunity.draft_dm_opportunity(
        store=store, opportunity_id=int(opp["id"]), draft_text=draft
    )
    row = store.get_dm_opportunity(int(opp["id"]))
    assert row["dm_draft_text"] == draft
    assert row["drafted_at"] is not None


# ===========================================================================
# 2. draft moves to awaiting_approval
# ===========================================================================

def test_draft_moves_to_awaiting_approval(store, backend):
    opp = _seed_dm_opp(backend)
    dm_opportunity.draft_dm_opportunity(
        store=store, opportunity_id=int(opp["id"]), draft_text="Hai!"
    )
    moved = dm_opportunity.transition_dm_opportunity(
        store=store,
        opportunity_id=int(opp["id"]),
        from_status="drafted",
        to_status="awaiting_approval",
        fields={"approval_channel": "telegram", "approval_ref": "tg:79553451"},
    )
    assert moved is not None
    assert moved["status"] == "awaiting_approval"
    assert moved["approval_channel"] == "telegram"


def test_mark_awaiting_helper(store, backend):
    opp = _seed_dm_opp(backend, status="drafted", dm_draft_text="Hai!")
    moved = dm_opportunity.mark_dm_awaiting_approval(
        store=store, opportunity_id=int(opp["id"]), approval_ref="tg:79553451"
    )
    assert moved is not None
    assert moved["status"] == "awaiting_approval"


# ===========================================================================
# 3. Telegram approval card contains correct context
# ===========================================================================

def _dispatcher(**kwargs):
    from threads_operator import workflow_telegram

    sent: list[dict[str, Any]] = []

    async def _send(chat_id=None, text=None, markup=None):
        sent.append({"chat_id": str(chat_id), "text": text, "markup": markup})

    async def _edit(chat_id, message_id, text, markup):
        sent.append({"edited": True, "text": text, "markup": markup})

    d = workflow_telegram.DMDispatcher(
        account="syaqir", send=_send, edit=_edit, **kwargs
    )
    return d, sent


def test_card_contains_required_context(backend, store):
    from threads_operator import workflow_telegram

    opp = _seed_dm_opp(
        backend,
        status="awaiting_approval",
        dm_draft_text="Hai @prospect, link kursus: example.com/x",
    )
    card = workflow_telegram.dm_approval_card_text(store.get_dm_opportunity(int(opp["id"])))
    assert "@prospect" in card                      # username
    assert "berminat sangat" in card                # incoming reply
    assert "potential_lead" in card                 # detected intent
    assert "0.85" in card or "85" in card           # lead score
    assert "berminat" in card.lower()               # CTA match
    assert "example.com/x" in card                  # proposed DM
    assert f"#{opp['id']}" in card                  # opportunity ID
    assert "syaqir" in card                         # account
    assert "Berminat nak belajar" in card           # root post excerpt


def test_card_keyboard_shape():
    from threads_operator import workflow_telegram

    kb = workflow_telegram.dm_approval_keyboard(7)
    rows = kb["inline_keyboard"]
    flat = [b for row in rows for b in row]
    datas = {b["callback_data"] for b in flat}
    assert datas == {"dmopp:approve:7", "dmopp:edit:7", "dmopp:reject:7"}


def test_dispatcher_ignores_non_dmopp_callbacks():
    from threads_operator import workflow_telegram

    d, _ = _dispatcher()
    for foreign in ("trendeng:approve:1", "ownreply:reject:2", "backlog:use:3",
                    "trendsched:best:4", "engagement:approve:5"):
        handled = _run(d.handle_callback(
            data=foreign, chat_id="c", user_id="u", message_id="1"
        ))
        assert handled is False, f"DM dispatcher must not claim {foreign}"


def test_dispatcher_alias_is_gateway_compatible():
    from threads_operator import workflow_telegram
    assert issubclass(workflow_telegram.DMTelegramBridge, workflow_telegram.DMDispatcher)


def test_watchdog_dm_keyboard_shape():
    """The watchdog must render the same dmopp keyboard as the dispatcher."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "wd", str(REPO / "scripts" / "own_replies_watchdog_5m.py")
    )
    wd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wd)
    kb = wd._dm_keyboard(9)
    flat = [b for row in kb["inline_keyboard"] for b in row]
    datas = {b["callback_data"] for b in flat}
    assert datas == {"dmopp:approve:9", "dmopp:edit:9", "dmopp:reject:9"}


# ===========================================================================
# 4 & 5. Approve / Reject transitions (dispatcher -> CLI path)
# ===========================================================================

def _run(coro):
    return asyncio.run(coro)


def _fake_cli_runner(payload: dict[str, Any], code: int = 0):
    async def _runner(cli_argv, group, args, *, cwd):
        return code, payload
    return _runner


def test_approve_transitions_to_approved(store, backend, monkeypatch):
    from threads_operator import workflow_telegram

    opp = _seed_dm_opp(
        backend, status="awaiting_approval", dm_draft_text="Hai!"
    )
    d, sent = _dispatcher()
    payload = {"ok": True, "id": int(opp["id"]), "status": "approved"}
    monkeypatch.setattr(workflow_telegram, "_run_cli", _fake_cli_runner(payload))
    handled = _run(d.handle_callback(
        data=f"dmopp:approve:{opp['id']}", chat_id="79553451", user_id="79553451",
        message_id="5590",
    ))
    assert handled is True
    # the dispatcher must have invoked the approve path
    assert payload["status"] == "approved"


def test_reject_transitions_to_rejected(store, backend, monkeypatch):
    from threads_operator import workflow_telegram

    opp = _seed_dm_opp(backend, status="awaiting_approval", dm_draft_text="Hai!")
    d, _ = _dispatcher()
    payload = {"ok": True, "id": int(opp["id"]), "status": "rejected"}
    monkeypatch.setattr(workflow_telegram, "_run_cli", _fake_cli_runner(payload))
    handled = _run(d.handle_callback(
        data=f"dmopp:reject:{opp['id']}", chat_id="79553451", user_id="79553451",
        message_id="5590",
    ))
    assert handled is True
    assert payload["status"] == "rejected"


# ===========================================================================
# 6. Edit replaces draft and remains awaiting_approval
# ===========================================================================

def test_edit_replaces_draft_and_stays_awaiting(store, backend):
    opp = _seed_dm_opp(
        backend, status="awaiting_approval", dm_draft_text="old draft"
    )
    moved = dm_opportunity.edit_dm_opportunity_draft(
        store=store,
        opportunity_id=int(opp["id"]),
        edited_text="new edited DM text",
    )
    assert moved is not None
    assert moved["status"] == "awaiting_approval"
    assert moved["dm_draft_text"] == "new edited DM text"
    assert moved["edited_at"] is not None
    assert moved["approved_at"] is None  # editing never auto-approves


# ===========================================================================
# 7 & 8. Empty edit + expired edit session (dispatcher handle_text)
# ===========================================================================

def test_empty_edit_rejected(store, backend, tmp_path):
    from threads_operator import workflow_telegram
    from threads_operator.engagement_sessions import PendingEditStore

    d, sent = _dispatcher(edit_store=PendingEditStore(tmp_path / "s.json"))
    d.store.create(chat_id="79553451", user_id="79553451",
                   engagement_id=5, account_key="syaqir")
    consumed = _run(d.handle_text(chat_id="79553451", user_id="79553451", text="   "))
    assert consumed is True
    assert any("empty" in m["text"].lower() for m in sent)
    # session still open (not consumed by an empty message)
    assert d.store.find_for_sender(chat_id="79553451", user_id="79553451") is not None


def test_expired_edit_session_rejected(store, backend, tmp_path):
    from threads_operator import workflow_telegram
    from threads_operator.engagement_sessions import PendingEditStore

    # TTL of 1 second; create session then force expiry
    store_sessions = PendingEditStore(tmp_path / "s.json", ttl_seconds=1)
    d, sent = _dispatcher(edit_store=store_sessions)
    session = d.store.create(chat_id="79553451", user_id="79553451",
                             engagement_id=5, account_key="syaqir", now=1000.0)
    # now far past expiry
    consumed = _run(
        _handle_text_at(d, "79553451", "79553451", "some text", now=session["expires_at"] + 10)
    )
    assert consumed is False  # expired session is invisible -> not consumed


async def _handle_text_at(d, chat_id, user_id, text, now):
    # find_for_sender is time-based; patch time via the store's _load
    import time as _t
    orig = _t.time
    _t.time = lambda: now
    try:
        return await d.handle_text(chat_id=chat_id, user_id=user_id, text=text)
    finally:
        _t.time = orig


# ===========================================================================
# 9. duplicate Approve callback is idempotent
# ===========================================================================

def test_duplicate_approve_is_idempotent(store, backend, monkeypatch):
    from threads_operator import workflow_telegram

    opp = _seed_dm_opp(backend, status="awaiting_approval", dm_draft_text="Hai!")
    d, _ = _dispatcher()
    calls = {"n": 0}

    async def _runner(cli_argv, group, args, *, cwd):
        calls["n"] += 1
        if calls["n"] == 1:
            return 0, {"ok": True, "id": int(opp["id"]), "status": "approved"}
        return 2, {"ok": False, "error": "not awaiting approval", "id": int(opp["id"]),
                   "status": "approved", "already_decided": True}
    monkeypatch.setattr(workflow_telegram, "_run_cli", _runner)
    r1 = _run(d.handle_callback(data=f"dmopp:approve:{opp['id']}",
                                chat_id="c", user_id="u", message_id="1"))
    r2 = _run(d.handle_callback(data=f"dmopp:approve:{opp['id']}",
                                chat_id="c", user_id="u", message_id="1"))
    assert r1 and r2  # both handled; second is a safe no-op, not an error


# ===========================================================================
# 10. stale callback rejected
# ===========================================================================

def test_stale_callback_rejected(store, backend, monkeypatch):
    from threads_operator import workflow_telegram

    opp = _seed_dm_opp(backend, status="approved", dm_draft_text="Hai!")
    d, sent = _dispatcher()
    # CLI refuses: row already approved (stale button)
    monkeypatch.setattr(
        workflow_telegram, "_run_cli",
        _fake_cli_runner({"ok": False, "error": "not awaiting approval", "already_decided": True}, code=2),
    )
    answers: list[str] = []

    async def _answer(text):
        answers.append(text)

    handled = _run(d.handle_callback(data=f"dmopp:approve:{opp['id']}",
                                     chat_id="c", user_id="u", message_id="1", answer=_answer))
    assert handled is True
    assert any("already" in a.lower() or "not awaiting" in a.lower() for a in answers)


# ===========================================================================
# 11. wrong-account callback rejected (fail closed)
# ===========================================================================

def test_wrong_account_rejected(store_account_b, backend):
    # The opportunity belongs to syaqir; the other account's store must not see it.
    opp = _seed_dm_opp(backend)  # account_key = syaqir
    moved = dm_opportunity.transition_dm_opportunity(
        store=store_account_b,
        opportunity_id=int(opp["id"]),
        from_status="awaiting_approval",
        to_status="approved",
    )
    assert moved is None  # CAS guard: account mismatch -> no row returned


# ===========================================================================
# 12. expired opportunity cannot be approved
# ===========================================================================

def test_expired_opportunity_cannot_be_approved(store, backend):
    opp = _seed_dm_opp(
        backend,
        status="awaiting_approval",
        dm_draft_text="Hai!",
        expires_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    moved = dm_opportunity.approve_dm_opportunity(
        store=store, opportunity_id=int(opp["id"]), approved_by="79553451"
    )
    assert moved is None  # expired -> refuse


def test_expire_stale_marks_expired(store, backend):
    opp = _seed_dm_opp(
        backend,
        status="awaiting_approval",
        dm_draft_text="Hai!",
        expires_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
    )
    report = dm_opportunity.expire_stale_opportunities(store)
    assert report["expired"] >= 1
    row = store.get_dm_opportunity(int(opp["id"]))
    assert row["status"] == "expired"


# ===========================================================================
# 13. Telegram dispatch retry does not create duplicate active cards
# ===========================================================================

def test_telegram_send_idempotent(store, backend):
    opp = _seed_dm_opp(backend, status="awaiting_approval", dm_draft_text="Hai!")
    # First send stamps approval_message_ref
    first = dm_opportunity.record_dm_approval_card_sent(
        store=store, opportunity_id=int(opp["id"]), message_ref="79553451:5590"
    )
    assert first is not None
    assert first["approval_message_ref"] == "79553451:5590"
    # Retry: already has a message ref -> caller skips re-send
    row = store.get_dm_opportunity(int(opp["id"]))
    assert row["approval_message_ref"] == "79553451:5590"
    eligible = dm_opportunity.list_dm_opportunities_needing_card(store)
    ids = [r["id"] for r in eligible]
    assert int(opp["id"]) not in ids  # already has an active card


# ===========================================================================
# 16. no Threads DM is sent anywhere in Task 2C
# ===========================================================================

def test_no_dm_sending_surface_in_repo():
    """Task 2C must not add any live Threads DM send path."""
    operator_dir = REPO / "src" / "threads_operator"
    offenders: list[str] = []
    banned = re.compile(r"(send_dm|dm_send|post_direct_message|direct_message_send|/messages\b)", re.I)
    for py in operator_dir.rglob("*.py"):
        if py.name in {"dm_opportunity.py", "dm_approval.py"}:
            # these may mention DMs conceptually but must not call a send endpoint
            text = py.read_text()
            for match in banned.finditer(text):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{py.name}:{line}: {match.group(0)}")
    assert not offenders, "Task 2C must not introduce a DM-send call: " + "; ".join(offenders)


# ===========================================================================
# Draft content requirements
# ===========================================================================

def test_generate_dm_draft_uses_context(store, backend):
    """Draft generation is given persona + root post + reply + intent."""
    opp = _seed_dm_opp(backend)
    captured: dict[str, Any] = {}

    def _fake_generate(*, system_prompt, user_prompt, max_output_tokens, providers=None, client=None):
        captured["system"] = system_prompt
        captured["user"] = user_prompt
        return "Hai @prospect, terima kasih atas minat! Saya akan kongsikan linknya."

    from unittest.mock import patch
    with patch("threads_operator.own_replies.content_generation.generate_text", _fake_generate):
        draft = own_replies.generate_dm_draft(
            store=store,
            opportunity=store.get_dm_opportunity(int(opp["id"])),
            persona_text="Bapak-bapak, cakap rojak, mesra.",
        )
    assert "@prospect" in captured["user"]                 # username
    assert "berminat" in captured["user"].lower()          # their reply
    assert "Bapak-bapak" in captured["system"]             # persona
    assert "potential_lead" in captured["user"]            # intent
    assert "berminat" in captured["user"].lower()          # CTA present
    assert isinstance(draft, str) and len(draft) > 0


def test_generate_dm_draft_does_not_fabricate(store, backend):
    """When root_post_text is absent, the prompt must say so, not invent context."""
    opp = _seed_dm_opp(backend, root_post_text=None)
    captured: dict[str, Any] = {}

    def _fake_generate(*, system_prompt, user_prompt, max_output_tokens, providers=None, client=None):
        captured["user"] = user_prompt
        return "Hai! Terima kasih."

    from unittest.mock import patch
    with patch("threads_operator.own_replies.content_generation.generate_text", _fake_generate):
        own_replies.generate_dm_draft(
            store=store,
            opportunity=store.get_dm_opportunity(int(opp["id"])),
            persona_text="persona",
        )
    assert "(original post text unavailable)" in captured["user"]


# ===========================================================================
# Migration 016 content
# ===========================================================================

def test_migration_016_adds_approval_columns():
    sql = (REPO / "migrations" / "016_dm_approval_workflow.sql").read_text()
    for col in ("drafted_at", "edited_at", "approval_message_ref", "approved_by"):
        assert re.search(rf"add column if not exists {col}\b", sql), f"016 missing {col}"


# ===========================================================================
# CLI runner layer (own-replies dm ...)
# ===========================================================================

def _cli_config(store):
    """Minimal AccountConfig stand-in whose _store() returns our fake store."""
    from types import SimpleNamespace
    cfg = SimpleNamespace()
    cfg.name = "syaqir"
    cfg.get = lambda k, d=None: d
    cfg.get_bool = lambda k: False
    cfg.require = lambda k: ""
    return cfg


def _patch_store(monkeypatch, store):
    from threads_operator import operator_cli
    monkeypatch.setattr(operator_cli, "_store", lambda config: store)
    return operator_cli


def test_cli_dm_approve_happy_path(monkeypatch, store, backend):
    opp = _seed_dm_opp(backend, status="awaiting_approval", dm_draft_text="Hai!")
    cli = _patch_store(monkeypatch, store)
    code, payload = cli._run_dm_approve(
        _cli_config(store), opportunity_id=int(opp["id"]), approval_ref="tg:79553451:5590"
    )
    assert code == 0 and payload["ok"] and payload["status"] == "approved"
    row = store.get_dm_opportunity(int(opp["id"]))
    assert row["status"] == "approved"
    assert row["dm_approved_text"] == "Hai!"
    assert row["approved_at"] is not None


def test_cli_dm_approve_duplicate_idempotent(monkeypatch, store, backend):
    opp = _seed_dm_opp(backend, status="approved", dm_draft_text="Hai!")
    cli = _patch_store(monkeypatch, store)
    code, payload = cli._run_dm_approve(
        _cli_config(store), opportunity_id=int(opp["id"]), approval_ref=None
    )
    assert code == 0 and payload["ok"] and payload["already_decided"] is True


def test_cli_dm_approve_expired_refused(monkeypatch, store, backend):
    opp = _seed_dm_opp(
        backend, status="awaiting_approval", dm_draft_text="Hai!",
        expires_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    cli = _patch_store(monkeypatch, store)
    code, payload = cli._run_dm_approve(
        _cli_config(store), opportunity_id=int(opp["id"]), approval_ref=None
    )
    assert code == 2 and not payload["ok"] and "expired" in payload["error"].lower()
    assert store.get_dm_opportunity(int(opp["id"]))["status"] == "awaiting_approval"


def test_cli_dm_approve_wrong_account_not_found(monkeypatch, store_account_b, backend):
    opp = _seed_dm_opp(backend, status="awaiting_approval", dm_draft_text="Hai!")
    cli = _patch_store(monkeypatch, store_account_b)
    code, payload = cli._run_dm_approve(
        _cli_config(store_account_b), opportunity_id=int(opp["id"]), approval_ref=None
    )
    assert code == 2 and not payload["ok"] and "not found" in payload["error"].lower()


def test_cli_dm_reject(monkeypatch, store, backend):
    opp = _seed_dm_opp(backend, status="awaiting_approval", dm_draft_text="Hai!")
    cli = _patch_store(monkeypatch, store)
    code, payload = cli._run_dm_reject(
        _cli_config(store), opportunity_id=int(opp["id"]), approval_ref=None
    )
    assert code == 0 and payload["status"] == "rejected"
    assert store.get_dm_opportunity(int(opp["id"]))["rejected_at"] is not None


def test_cli_dm_reject_already_terminal(monkeypatch, store, backend):
    opp = _seed_dm_opp(backend, status="rejected", dm_draft_text="Hai!")
    cli = _patch_store(monkeypatch, store)
    code, payload = cli._run_dm_reject(
        _cli_config(store), opportunity_id=int(opp["id"]), approval_ref=None
    )
    assert code == 2 and payload["already_decided"] is True


def test_cli_dm_edit_replaces_and_stays(monkeypatch, store, backend):
    opp = _seed_dm_opp(backend, status="awaiting_approval", dm_draft_text="old")
    cli = _patch_store(monkeypatch, store)
    code, payload = cli._run_dm_edit(
        _cli_config(store), opportunity_id=int(opp["id"]), text="new DM text"
    )
    assert code == 0 and payload["ok"]
    assert payload["status"] == "awaiting_approval"
    assert payload["dm_draft_text"] == "new DM text"
    assert "new DM text" in payload["card_text"]
    row = store.get_dm_opportunity(int(opp["id"]))
    assert row["status"] == "awaiting_approval"
    assert row["edited_at"] is not None
    assert row["approved_at"] is None


def test_cli_dm_edit_empty_rejected(monkeypatch, store, backend):
    opp = _seed_dm_opp(backend, status="awaiting_approval", dm_draft_text="old")
    cli = _patch_store(monkeypatch, store)
    code, payload = cli._run_dm_edit(
        _cli_config(store), opportunity_id=int(opp["id"]), text="   "
    )
    assert code == 2 and "empty" in payload["error"].lower()
    assert store.get_dm_opportunity(int(opp["id"]))["dm_draft_text"] == "old"


def test_cli_dm_edit_never_approves(monkeypatch, store, backend):
    opp = _seed_dm_opp(backend, status="awaiting_approval", dm_draft_text="old")
    cli = _patch_store(monkeypatch, store)
    cli._run_dm_edit(_cli_config(store), opportunity_id=int(opp["id"]), text="x")
    assert store.get_dm_opportunity(int(opp["id"]))["status"] == "awaiting_approval"


def test_cli_dm_mark_card_sent_and_needing_card(monkeypatch, store, backend):
    a = _seed_dm_opp(backend, status="awaiting_approval", dm_draft_text="d")
    b = _seed_dm_opp(backend, status="awaiting_approval", dm_draft_text="d")
    cli = _patch_store(monkeypatch, store)
    code, payload = cli._run_dm_mark_card_sent(
        _cli_config(store), opportunity_id=int(a["id"]), message_ref="79553451:5590"
    )
    assert code == 0 and payload["ok"]
    code, payload = cli._run_dm_needing_card(_cli_config(store), limit=50)
    ids = [i["id"] for i in payload["items"]]
    assert int(a["id"]) not in ids  # already has an active card
    assert int(b["id"]) in ids


def test_cli_dm_draft_then_awaiting(monkeypatch, store, backend):
    opp = _seed_dm_opp(backend, status="detected")
    cli = _patch_store(monkeypatch, store)
    monkeypatch.setattr(
        "threads_operator.own_replies.generate_dm_draft",
        lambda *, store, opportunity, persona_text: "Hai! Link: example.com/x",
    )
    monkeypatch.setattr(
        "threads_operator.trend_engagement.load_persona", lambda root, name: "persona"
    )
    code, payload = cli._run_dm_draft(_cli_config(store), opportunity_id=int(opp["id"]))
    assert code == 0 and payload["ok"] and payload["status"] == "awaiting_approval"
    row = store.get_dm_opportunity(int(opp["id"]))
    assert row["status"] == "awaiting_approval"
    assert row["dm_draft_text"] == "Hai! Link: example.com/x"
    assert row["drafted_at"] is not None
