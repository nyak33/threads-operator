import asyncio

import pytest

from threads_operator.engagement_sessions import PendingEditStore
from threads_operator.engagement_telegram import (
    EngagementTelegramBridge,
    engagement_card_text,
    engagement_keyboard,
    parse_callback,
)


def run(coro):
    return asyncio.run(coro)


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.answered = []

    async def send(self, chat_id, text, markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "markup": markup})

    async def edit(self, chat_id, message_id, text, markup):
        self.edited.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text, "markup": markup}
        )

    async def answer(self, text):
        self.answered.append(text)


class FakeRunner:
    """Scripted CLI subprocess stand-in: (rc, payload) per invocation."""

    def __init__(self):
        self.calls = []
        self.responses = []

    def push(self, rc, payload):
        self.responses.append((rc, payload))

    async def __call__(self, cli_argv, cli_args, *, cwd, timeout):
        self.calls.append(list(cli_args))
        if not self.responses:
            raise AssertionError(f"unexpected CLI call: {cli_args}")
        return self.responses.pop(0)


def make_bridge(tmp_path, runner, *, env=None):
    tg = FakeTelegram()
    bridge = EngagementTelegramBridge(
        account="syaqir",
        send=tg.send,
        edit=tg.edit,
        store=PendingEditStore(tmp_path / "sessions.json"),
        cli_argv=["fake-cli"],
        cli_cwd="/repo",
        runner=runner,
        env={} if env is None else env,
    )
    return bridge, tg


PENDING_ROW = {
    "id": 1,
    "account_key": "syaqir",
    "source_username": "sairahjerr",
    "source_text": "source preview",
    "proposed_text": "draft reply",
    "status": "pending_approval",
}


# --- callback parsing -------------------------------------------------------

@pytest.mark.parametrize(
    "data,expected",
    [
        ("engagement:approve:1", ("approve", 1)),
        ("engagement:edit:42", ("edit", 42)),
        ("engagement:reject:7", ("reject", 7)),
        ("engagement:APPROVE:3", ("approve", 3)),
    ],
)
def test_parse_callback_valid(data, expected):
    assert parse_callback(data) == expected


@pytest.mark.parametrize(
    "data",
    [
        None,
        "",
        "engagement:",
        "engagement:delete:1",
        "engagement:approve:abc",
        "engagement:approve:0",
        "engagement:approve:-1",
        "engagement:approve:1:extra",
        "mp:model:1",
        "ea:once:12",
        "other",
    ],
)
def test_parse_callback_rejects_garbage(data):
    assert parse_callback(data) is None


# --- approve ---------------------------------------------------------------

def test_approve_maps_to_cli_and_updates_card_when_execution_disabled(tmp_path):
    runner = FakeRunner()
    runner.push(0, {"ok": True, "action": {**PENDING_ROW, "status": "approved"}})
    bridge, tg = make_bridge(tmp_path, runner, env={"THREADS_ENGAGEMENT_ENABLED": "false"})

    handled = run(
        bridge.handle_callback(
            data="engagement:approve:1",
            chat_id="79553451",
            user_id="79553451",
            message_id="3425",
            answer=tg.answer,
        )
    )

    assert handled is True
    assert runner.calls == [
        ["approve", "--account", "syaqir", "--id", "1", "--approval-ref", "tg:79553451:3425"]
    ]
    assert tg.answered == ["✅ Approved — execution disabled."]
    assert len(tg.edited) == 1
    assert "execution is disabled" in tg.edited[0]["text"]
    assert tg.edited[0]["markup"] is None


def test_approve_executes_only_after_successful_approval_when_enabled(tmp_path):
    runner = FakeRunner()
    runner.push(0, {"ok": True, "action": {**PENDING_ROW, "status": "approved"}})
    runner.push(
        0,
        {
            "ok": True,
            "status": "posted",
            "reply_id": "18123456789",
            "action": {**PENDING_ROW, "status": "posted", "external_action_id": "18123456789"},
        },
    )
    bridge, tg = make_bridge(tmp_path, runner, env={"THREADS_ENGAGEMENT_ENABLED": "true"})

    run(
        bridge.handle_callback(
            data="engagement:approve:1",
            chat_id="79553451",
            user_id="79553451",
            message_id="3425",
            answer=tg.answer,
        )
    )

    assert runner.calls[0][0] == "approve"
    assert runner.calls[1] == ["execute", "--account", "syaqir", "--id", "1"]
    assert "18123456789" in tg.edited[0]["text"]


def test_approve_failure_never_executes(tmp_path):
    runner = FakeRunner()
    runner.push(2, {"ok": False, "error": "Reply is not pending approval or does not belong to this account"})
    bridge, tg = make_bridge(tmp_path, runner, env={"THREADS_ENGAGEMENT_ENABLED": "true"})

    run(
        bridge.handle_callback(
            data="engagement:approve:1",
            chat_id="79553451",
            user_id="79553451",
            message_id="3425",
            answer=tg.answer,
        )
    )

    assert len(runner.calls) == 1  # execute never attempted
    assert tg.answered == ["Already resolved — no longer pending."]
    assert tg.edited == []


def test_duplicate_approve_callback_is_safe(tmp_path):
    runner = FakeRunner()
    runner.push(0, {"ok": True, "action": {**PENDING_ROW, "status": "approved"}})
    runner.push(2, {"ok": False, "error": "Reply is not pending approval or does not belong to this account"})
    bridge, tg = make_bridge(tmp_path, runner, env={"THREADS_ENGAGEMENT_ENABLED": "false"})

    for _ in range(2):
        run(
            bridge.handle_callback(
                data="engagement:approve:1",
                chat_id="79553451",
                user_id="79553451",
                message_id="3425",
                answer=tg.answer,
            )
        )

    assert tg.answered[1] == "Already resolved — no longer pending."
    assert len(tg.edited) == 1


# --- reject / skip ----------------------------------------------------------

def test_reject_maps_to_cli_and_marks_card(tmp_path):
    runner = FakeRunner()
    runner.push(0, {"ok": True, "action": {**PENDING_ROW, "status": "rejected"}})
    bridge, tg = make_bridge(tmp_path, runner)

    handled = run(
        bridge.handle_callback(
            data="engagement:reject:1",
            chat_id="79553451",
            user_id="79553451",
            message_id="3425",
            answer=tg.answer,
        )
    )

    assert handled is True
    assert runner.calls == [
        ["reject", "--account", "syaqir", "--id", "1", "--approval-ref", "tg:79553451:3425"]
    ]
    assert tg.answered == ["❌ Rejected."]
    assert "Rejected / skipped" in tg.edited[0]["text"]


# --- edit session flow ------------------------------------------------------

def test_edit_callback_creates_session_and_prompts(tmp_path):
    runner = FakeRunner()
    runner.push(0, {"ok": True, "count": 1, "actions": [PENDING_ROW]})
    bridge, tg = make_bridge(tmp_path, runner)

    handled = run(
        bridge.handle_callback(
            data="engagement:edit:1",
            chat_id="79553451",
            user_id="79553451",
            message_id="3425",
            answer=tg.answer,
        )
    )

    assert handled is True
    assert runner.calls[0][0:2] == ["list", "--account"]
    session = bridge.store.find_for_sender(chat_id="79553451", user_id="79553451")
    assert session is not None and session["engagement_id"] == 1
    assert session["account_key"] == "syaqir"
    assert any("replacement reply" in m["text"] for m in tg.sent)


def test_edit_callback_on_stale_row_opens_no_session(tmp_path):
    runner = FakeRunner()
    runner.push(0, {"ok": True, "count": 0, "actions": []})
    bridge, tg = make_bridge(tmp_path, runner)

    run(
        bridge.handle_callback(
            data="engagement:edit:1",
            chat_id="79553451",
            user_id="79553451",
            message_id="3425",
            answer=tg.answer,
        )
    )

    assert tg.answered == ["Already resolved — no longer pending."]
    assert bridge.store.find_for_sender(chat_id="79553451", user_id="79553451") is None
    assert tg.sent == []


def _open_session(bridge, runner):
    runner.push(0, {"ok": True, "count": 1, "actions": [PENDING_ROW]})
    run(
        bridge.handle_callback(
            data="engagement:edit:1",
            chat_id="79553451",
            user_id="79553451",
            message_id="3425",
            answer=None,
        )
    )


def test_next_message_runs_cli_edit_keeps_pending_and_refreshes_card(tmp_path):
    runner = FakeRunner()
    bridge, tg = make_bridge(tmp_path, runner)
    _open_session(bridge, runner)
    runner.push(0, {"ok": True, "action": {**PENDING_ROW, "proposed_text": "new reply text"}})

    consumed = run(
        bridge.handle_text(chat_id="79553451", user_id="79553451", text="new reply text")
    )

    assert consumed is True
    assert runner.calls[-1] == [
        "edit", "--account", "syaqir", "--id", "1", "--text", "new reply text",
    ]
    # Session cleared after successful edit.
    assert bridge.store.find_for_sender(chat_id="79553451", user_id="79553451") is None
    # Refreshed card still offers approval — edit did not approve.
    last = tg.sent[-1]
    assert "still pending approval" in last["text"]
    assert last["markup"] == engagement_keyboard(1)
    assert runner.calls[-1][0] == "edit" and "approve" not in runner.calls[-1]


def test_cancel_clears_session_without_touching_row(tmp_path):
    runner = FakeRunner()
    bridge, tg = make_bridge(tmp_path, runner)
    _open_session(bridge, runner)
    calls_before = len(runner.calls)

    consumed = run(bridge.handle_text(chat_id="79553451", user_id="79553451", text="cancel"))

    assert consumed is True
    assert len(runner.calls) == calls_before  # no CLI call for cancel
    assert bridge.store.find_for_sender(chat_id="79553451", user_id="79553451") is None
    assert "cancelled" in tg.sent[-1]["text"]


def test_wrong_user_message_passes_through(tmp_path):
    runner = FakeRunner()
    bridge, tg = make_bridge(tmp_path, runner)
    _open_session(bridge, runner)

    consumed = run(bridge.handle_text(chat_id="79553451", user_id="99999999", text="hijack attempt"))

    assert consumed is False
    assert bridge.store.find_for_sender(chat_id="79553451", user_id="79553451") is not None


def test_message_without_session_passes_through(tmp_path):
    runner = FakeRunner()
    bridge, _ = make_bridge(tmp_path, runner)

    consumed = run(bridge.handle_text(chat_id="79553451", user_id="79553451", text="hello bot"))

    assert consumed is False
    assert runner.calls == []


def test_expired_session_does_not_consume_message(tmp_path):
    runner = FakeRunner()
    bridge, tg = make_bridge(tmp_path, runner)
    _open_session(bridge, runner)
    # Force expiry by rewriting TTL horizon.
    session = bridge.store.find_for_sender(chat_id="79553451", user_id="79553451")
    assert session is not None
    bridge.store.remove(session["token"])
    bridge.store.create(
        chat_id="79553451",
        user_id="79553451",
        engagement_id=1,
        account_key="syaqir",
        now=1000.0,
    )

    consumed = run(bridge.handle_text(chat_id="79553451", user_id="79553451", text="late text"))
    # The ancient session (created at t=1000) is expired relative to wall clock.
    assert consumed is False
    assert bridge.store.find_for_sender(chat_id="79553451", user_id="79553451") is None


def test_edit_cli_failure_keeps_session_open(tmp_path):
    runner = FakeRunner()
    bridge, tg = make_bridge(tmp_path, runner)
    _open_session(bridge, runner)
    runner.push(1, {"ok": False, "error": "network boom"})

    consumed = run(bridge.handle_text(chat_id="79553451", user_id="79553451", text="new text"))

    assert consumed is True
    assert "Edit failed" in tg.sent[-1]["text"]
    assert bridge.store.find_for_sender(chat_id="79553451", user_id="79553451") is not None


# --- misc -------------------------------------------------------------------

def test_non_engagement_callback_is_ignored(tmp_path):
    runner = FakeRunner()
    bridge, _ = make_bridge(tmp_path, runner)

    handled = run(
        bridge.handle_callback(
            data="ea:once:12", chat_id="1", user_id="1", message_id="2", answer=None
        )
    )

    assert handled is False
    assert runner.calls == []


def test_handler_exception_surfaces_clean_answer_not_crash(tmp_path):
    class ExplodingRunner:
        async def __call__(self, *args, **kwargs):
            raise RuntimeError("boom")

    bridge, tg = make_bridge(tmp_path, ExplodingRunner())
    handled = run(
        bridge.handle_callback(
            data="engagement:approve:1",
            chat_id="1",
            user_id="1",
            message_id="2",
            answer=tg.answer,
        )
    )
    assert handled is True
    assert tg.answered == ["❌ Handler error — check gateway logs."]


def test_card_text_and_keyboard_shape():
    text = engagement_card_text(PENDING_ROW, note="note here")
    assert "@sairahjerr" in text and "engagement #1" in text and "note here" in text
    keyboard = engagement_keyboard(1)
    buttons = keyboard["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == [
        "engagement:approve:1",
        "engagement:edit:1",
        "engagement:reject:1",
    ]
