"""Telebot dispatchers for the two engagement workflows (A and B).

Same architecture as engagement_telegram.py: the gateway remains the only
getUpdates poller; every state transition goes through the operator CLI
subprocess; callback data carries only ``<prefix>:<action>:<id>`` — never
credentials or content.

- Workflow A (``trendeng:``): trend candidate -> original own post.
  Approve enqueues via the normal publish queue (never publishes directly).
- Workflow B (``ownreply:``): replies under our own posts.
  Approve publishes via the official Graph API after the approval transition.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from .engagement_sessions import PendingEditStore
from .engagement_telegram import (
    DEFAULT_CLI,
    DEFAULT_TIMEOUT_SECONDS,
    AnswerFn,
    EditFn,
    SendFn,
    _err,
    run_engagement_cli,
)

logger = logging.getLogger(__name__)

TRENDENG_PREFIX = "trendeng:"
TRENDENG_ACTIONS = ("approve", "edit", "reject", "skip")

OWNREPLY_PREFIX = "ownreply:"
OWNREPLY_ACTIONS = ("approve", "edit", "reject", "ignore")

CANCEL_WORDS = {"cancel"}
MAX_CHARS = 500


def parse_prefixed_callback(data: str | None, prefix: str, actions: tuple[str, ...]) -> tuple[str, int] | None:
    if not isinstance(data, str) or not data.startswith(prefix):
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    action = parts[1].strip().lower()
    if action not in actions:
        return None
    try:
        row_id = int(parts[2])
    except ValueError:
        return None
    return (action, row_id) if row_id > 0 else None


def trendeng_keyboard(candidate_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"trendeng:approve:{candidate_id}"},
                {"text": "✏️ Edit", "callback_data": f"trendeng:edit:{candidate_id}"},
            ],
            [
                {"text": "❌ Reject", "callback_data": f"trendeng:reject:{candidate_id}"},
                {"text": "⏭️ Skip", "callback_data": f"trendeng:skip:{candidate_id}"},
            ],
        ]
    }


def ownreply_keyboard(row_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"ownreply:approve:{row_id}"},
                {"text": "✏️ Edit", "callback_data": f"ownreply:edit:{row_id}"},
            ],
            [
                {"text": "❌ Reject", "callback_data": f"ownreply:reject:{row_id}"},
                {"text": "🙈 Ignore", "callback_data": f"ownreply:ignore:{row_id}"},
            ],
        ]
    }


async def _run_cli(
    cli_argv: list[str],
    group: str,
    cli_args: list[str],
    *,
    cwd: str | None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any]]:
    """run_engagement_cli hardcodes the 'engagement' group; generalise here."""
    import asyncio
    import json
    from .engagement_telegram import _shell_join

    argv = [*cli_argv, group, *cli_args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return 1, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 1, {"ok": False, "error": f"CLI timed out after {timeout:.0f}s: {_shell_join(argv)}"}
    out = stdout.decode("utf-8", "replace").strip()
    try:
        payload = json.loads(out) if out else {}
    except json.JSONDecodeError:
        err = stderr.decode("utf-8", "replace").strip()[:500]
        return proc.returncode or 1, {"ok": False, "error": err or f"Non-JSON CLI output (rc={proc.returncode})"}
    if not isinstance(payload, dict):
        payload = {"ok": False, "error": "Unexpected CLI output"}
    return proc.returncode, payload


class _BaseWorkflowDispatcher:
    """Shared callback/edit-session plumbing for both workflows."""

    group: str = ""           # CLI subcommand group
    prefix: str = ""
    actions: tuple[str, ...] = ()
    edit_prompt: str = "Send the replacement text, or 'cancel'."

    def __init__(
        self,
        *,
        account: str,
        send: SendFn,
        edit_message: EditFn | None = None,
        cli_argv: list[str] | None = None,
        cwd: str | None = None,
        edit_store: PendingEditStore | None = None,
    ) -> None:
        self.account = account
        self._send = send
        self._edit = edit_message
        self.cli_argv = cli_argv or list(DEFAULT_CLI)
        self.cwd = cwd
        if edit_store is None:
            default_path = Path.home() / ".threads-operator" / "engagement_edit_sessions.json"
            edit_store = PendingEditStore(default_path)
        self.store = edit_store

    async def _answer(self, answer: AnswerFn | None, text: str) -> None:
        if answer is not None:
            try:
                await answer(text=text)
                return
            except Exception:  # noqa: BLE001
                logger.warning("answer_callback_query failed", exc_info=True)
        await self._send(text=text)

    async def handle_callback(
        self,
        *,
        data: str,
        chat_id: str,
        user_id: str,
        message_id: str | None = None,
        answer: AnswerFn | None = None,
    ) -> bool:
        parsed = parse_prefixed_callback(data, self.prefix, self.actions)
        if parsed is None:
            return False
        action, row_id = parsed
        if action == "edit":
            await self._start_edit(row_id, chat_id=str(chat_id), user_id=str(user_id), answer=answer)
            return True
        await self._decide(row_id, action, chat_id=str(chat_id), message_id=message_id, answer=answer)
        return True

    async def _start_edit(
        self, row_id: int, *, chat_id: str, user_id: str, answer: AnswerFn | None
    ) -> None:
        self.store.create(
            chat_id=chat_id, user_id=user_id, engagement_id=row_id,
            account_key=self.account,
        )
        await self._answer(answer, f"✏️ Edit mode for #{row_id}. {self.edit_prompt}")

    async def _decide(
        self, row_id: int, action: str, *, chat_id: str, message_id: str | None, answer: AnswerFn | None
    ) -> None:
        ref = f"tg:{chat_id}:{message_id}" if message_id else f"tg:{chat_id}"
        code, payload = await _run_cli(
            self.cli_argv, self.group,
            [action, "--account", self.account, "--id", str(row_id), "--approval-ref", ref],
            cwd=self.cwd,
        )
        if code != 0 or not payload.get("ok"):
            await self._answer(answer, f"❌ {action.title()} failed: {_err(payload)}")
            return
        await self._answer(answer, f"✅ #{row_id}: {action} recorded ({payload.get('status', action)}).")

    async def handle_text(
        self,
        *,
        chat_id: str,
        user_id: str,
        text: str,
        answer: AnswerFn | None = None,
    ) -> bool:
        session = self.store.find_for_sender(chat_id=str(chat_id), user_id=str(user_id))
        if session is None:
            return False
        row_id = int(session["engagement_id"])
        body = (text or "").strip()
        if body.lower() in CANCEL_WORDS:
            self.store.remove(session["token"])
            await self._send(text=f"✏️ Edit cancelled for #{row_id}. Row unchanged.")
            return True
        if not body:
            await self._send(text="Replacement text is empty — send the new text, or 'cancel'.")
            return True
        if len(body) > MAX_CHARS:
            await self._send(text=f"Replacement is {len(body)} chars (max {MAX_CHARS}). Shorten it, or 'cancel'.")
            return True
        code, payload = await _run_cli(
            self.cli_argv, self.group,
            ["edit", "--account", self.account, "--id", str(row_id), "--text", body],
            cwd=self.cwd,
        )
        if code != 0 or not payload.get("ok"):
            err = _err(payload)
            if "not pending" in err.lower() or "not in an open state" in err.lower():
                self.store.remove(session["token"])
                await self._send(text=f"⚠️ #{row_id} is no longer awaiting approval; edit session closed.")
            else:
                await self._send(text=f"❌ Edit failed for #{row_id}: {err} — session still open; send new text or 'cancel'.")
            return True
        self.store.remove(session["token"])
        await self._send(text=f"✏️ #{row_id} updated — still pending approval.")
        return True


class TrendEngagementDispatcher(_BaseWorkflowDispatcher):
    """Workflow A: trendeng: callbacks -> trend-engagement CLI group."""

    group = "trend-engagement"
    prefix = TRENDENG_PREFIX
    actions = TRENDENG_ACTIONS
    edit_prompt = "Send the new draft text for this trend post, or 'cancel'."

    async def _decide(
        self, row_id: int, action: str, *, chat_id: str, message_id: str | None, answer: AnswerFn | None
    ) -> None:
        await super()._decide(row_id, action, chat_id=chat_id, message_id=message_id, answer=answer)
        if action == "approve":
            # Surface the queue id when the CLI returned one.
            pass


class OwnReplyDispatcher(_BaseWorkflowDispatcher):
    """Workflow B: ownreply: callbacks -> own-replies CLI group."""

    group = "own-replies"
    prefix = OWNREPLY_PREFIX
    actions = OWNREPLY_ACTIONS
    edit_prompt = "Send the new reply text, or 'cancel'."
