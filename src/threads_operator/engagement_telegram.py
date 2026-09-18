"""Telebot callback dispatcher for approval-gated Threads engagement.

Thin async bridge between the Hermes Telegram gateway adapter and the
``threads-operator engagement`` CLI. The gateway stays the ONLY getUpdates
poller for the bot token; this module never polls Telegram and never writes
to Supabase directly — every state transition goes through the operator CLI
subprocess, which remains the authoritative state machine.

Callback payloads are exactly ``engagement:<action>:<id>`` with action in
{approve, edit, reject}. No credentials or tokens ever enter callback data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from pathlib import Path
from typing import Any, Awaitable, Callable

from .engagement_sessions import PendingEditStore

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "engagement:"
ACTIONS = ("approve", "edit", "reject")
CANCEL_WORDS = {"cancel"}
MAX_REPLY_CHARS = 500

DEFAULT_CLI = [".venv/bin/threads-operator"]
DEFAULT_TIMEOUT_SECONDS = 60

SendFn = Callable[..., Awaitable[Any]]
EditFn = Callable[..., Awaitable[Any]]
AnswerFn = Callable[..., Awaitable[Any]]
RunnerFn = Callable[..., Awaitable[tuple[int, dict[str, Any]]]]


def parse_callback(data: str | None) -> tuple[str, int] | None:
    """Parse ``engagement:<action>:<id>``; ``None`` for anything else."""
    if not isinstance(data, str) or not data.startswith(CALLBACK_PREFIX):
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    action = parts[1].strip().lower()
    if action not in ACTIONS:
        return None
    try:
        engagement_id = int(parts[2])
    except ValueError:
        return None
    if engagement_id <= 0:
        return None
    return action, engagement_id


def _shell_join(argv: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in argv)


async def run_engagement_cli(
    cli_argv: list[str],
    cli_args: list[str],
    *,
    cwd: str | None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any]]:
    """Run ``threads-operator engagement <cli_args>`` and parse its JSON stdout."""
    argv = [*cli_argv, "engagement", *cli_args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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


def _is_not_pending(payload: dict[str, Any]) -> bool:
    return "not pending approval" in str(payload.get("error", "")).lower()


def _err(payload: dict[str, Any], fallback: str = "unknown error") -> str:
    return str(payload.get("error") or fallback)


def engagement_card_text(action: dict[str, Any], *, note: str | None = None) -> str:
    username = action.get("source_username") or "unknown"
    source = str(action.get("source_text") or "").strip()
    preview = source if len(source) <= 240 else source[:237] + "..."
    proposed = str(action.get("proposed_text") or "").strip()
    lines = [
        "Reply approval",
        "",
        f"@{username} — engagement #{action.get('id')}",
        "",
        "Original:",
        preview or "(no source preview)",
        "",
        "Suggested reply:",
        proposed or "(empty)",
    ]
    reason = str(action.get("reason") or "").strip()
    if reason:
        lines += ["", "Reason:", reason]
    if note:
        lines += ["", note]
    return "\n".join(lines)


def engagement_keyboard(engagement_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"engagement:approve:{engagement_id}"},
                {"text": "✏️ Edit", "callback_data": f"engagement:edit:{engagement_id}"},
                {"text": "❌ Skip", "callback_data": f"engagement:reject:{engagement_id}"},
            ]
        ]
    }


def engagement_result_text(action: dict[str, Any], note: str) -> str:
    username = action.get("source_username") or "unknown"
    proposed = str(action.get("proposed_text") or "").strip()
    return "\n".join(
        [
            f"Reply approval — engagement #{action.get('id')} (@{username})",
            "",
            "Suggested reply:",
            proposed or "(empty)",
            "",
            note,
        ]
    )


class EngagementTelegramBridge:
    """Maps Telebot button taps and edit-session replies to CLI transitions."""

    def __init__(
        self,
        *,
        account: str,
        send: SendFn,
        edit: EditFn | None = None,
        store: PendingEditStore | None = None,
        cli_argv: list[str] | None = None,
        cli_cwd: str | None = None,
        runner: RunnerFn | None = None,
        env: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.account = account
        self._send = send
        self._edit = edit
        self.store = store or self._default_store()
        self.cli_argv = list(cli_argv) if cli_argv else list(DEFAULT_CLI)
        self.cli_cwd = cli_cwd if cli_cwd is not None else self._default_cwd()
        self._runner = runner
        self.env = dict(os.environ if env is None else env)
        self.timeout = timeout

    @staticmethod
    def _default_store() -> PendingEditStore:
        path = os.environ.get("THREADS_ENGAGEMENT_EDIT_SESSIONS_PATH")
        if path:
            return PendingEditStore(Path(path))
        return PendingEditStore(Path.home() / ".threads-operator" / "engagement_edit_sessions.json")

    @staticmethod
    def _default_cwd() -> str | None:
        here = Path(__file__).resolve()
        for parent in here.parents:
            if (parent / "pyproject.toml").exists():
                return str(parent)
        return None

    async def _run(self, *cli_args: str) -> tuple[int, dict[str, Any]]:
        if self._runner is not None:
            return await self._runner(list(self.cli_argv), list(cli_args), cwd=self.cli_cwd, timeout=self.timeout)
        return await run_engagement_cli(
            self.cli_argv, list(cli_args), cwd=self.cli_cwd, timeout=self.timeout
        )

    def _engagement_enabled(self) -> bool:
        return self.env.get("THREADS_ENGAGEMENT_ENABLED", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    async def _answer(self, answer: AnswerFn | None, text: str) -> None:
        if answer is None:
            return
        try:
            await answer(text)
        except Exception:
            logger.warning("Telebot answer_callback_query failed", exc_info=True)

    async def _edit_card(self, chat_id: str, message_id: str | None, text: str, markup: dict | None) -> None:
        if not message_id or self._edit is None:
            return
        try:
            await self._edit(chat_id, message_id, text, markup)
        except Exception:
            logger.warning("Telebot card edit failed; state already changed via CLI", exc_info=True)

    async def handle_callback(
        self,
        *,
        data: str,
        chat_id: str,
        user_id: str,
        message_id: str | None = None,
        answer: AnswerFn | None = None,
    ) -> bool:
        """Handle one inline-button tap. ``False`` = not an engagement callback."""
        parsed = parse_callback(data)
        if parsed is None:
            return False
        action, engagement_id = parsed
        handler = {
            "approve": self._handle_approve,
            "edit": self._handle_edit,
            "reject": self._handle_reject,
        }[action]
        try:
            await handler(
                engagement_id,
                chat_id=str(chat_id),
                user_id=str(user_id),
                message_id=message_id,
                answer=answer,
            )
        except Exception:
            logger.exception("engagement %s callback failed for id=%s", action, engagement_id)
            await self._answer(answer, "❌ Handler error — check gateway logs.")
        return True

    async def handle_text(
        self,
        *,
        chat_id: str,
        user_id: str,
        text: str,
        answer: AnswerFn | None = None,
    ) -> bool:
        """Intercept the next message of a pending edit session.

        ``True`` means the message was consumed as edit input (or cancel) and
        must NOT reach normal agent handling. ``False`` = no live session for
        this chat+user; the message passes through untouched.
        """
        session = self.store.find_for_sender(chat_id=str(chat_id), user_id=str(user_id))
        if session is None:
            return False
        engagement_id = int(session["engagement_id"])
        body = (text or "").strip()
        if body.lower() in CANCEL_WORDS:
            self.store.remove(session["token"])
            await self._send(
                str(chat_id),
                f"✏️ Edit cancelled for engagement #{engagement_id}. Row unchanged.",
            )
            return True
        if not body:
            await self._send(str(chat_id), "Replacement text is empty — send the new reply text, or 'cancel'.")
            return True
        if len(body) > MAX_REPLY_CHARS:
            await self._send(
                str(chat_id),
                f"Replacement is {len(body)} chars (max {MAX_REPLY_CHARS}). Shorten it, or send 'cancel'.",
            )
            return True
        code, payload = await self._run(
            "edit", "--account", self.account, "--id", str(engagement_id), "--text", body
        )
        if code != 0 or not payload.get("ok"):
            if _is_not_pending(payload):
                self.store.remove(session["token"])
                await self._send(
                    str(chat_id),
                    f"⚠️ Engagement #{engagement_id} is no longer pending approval; edit session closed.",
                )
            else:
                await self._send(
                    str(chat_id),
                    f"❌ Edit failed for engagement #{engagement_id}: {_err(payload)} — edit session still open; send new text or 'cancel'.",
                )
            return True
        self.store.remove(session["token"])
        action = payload.get("action") or {"id": engagement_id}
        await self._send(
            str(chat_id),
            engagement_card_text(action, note="✏️ Reply updated — still pending approval."),
            markup=engagement_keyboard(engagement_id),
        )
        return True

    async def _handle_approve(
        self,
        engagement_id: int,
        *,
        chat_id: str,
        user_id: str,
        message_id: str | None,
        answer: AnswerFn | None,
    ) -> None:
        ref = f"tg:{chat_id}:{message_id}" if message_id else f"tg:{chat_id}"
        code, payload = await self._run(
            "approve", "--account", self.account, "--id", str(engagement_id), "--approval-ref", ref
        )
        if code != 0 or not payload.get("ok"):
            if _is_not_pending(payload):
                await self._answer(answer, "Already resolved — no longer pending.")
            else:
                await self._answer(answer, f"❌ Approve failed: {_err(payload)}")
            return
        action = payload.get("action") or {"id": engagement_id}
        if not self._engagement_enabled():
            await self._answer(answer, "✅ Approved — execution disabled.")
            await self._edit_card(
                chat_id,
                message_id,
                engagement_result_text(
                    action,
                    "✅ Approved — live execution is disabled (THREADS_ENGAGEMENT_ENABLED=false). Not published.",
                ),
                None,
            )
            return
        await self._answer(answer, "✅ Approved — executing…")
        ecode, epayload = await self._run("execute", "--account", self.account, "--id", str(engagement_id))
        if ecode == 0 and epayload.get("ok"):
            reply_id = epayload.get("reply_id") or (epayload.get("action") or {}).get("external_action_id")
            note = f"✅ Posted — reply ID {reply_id}." if reply_id else "✅ Posted."
            await self._edit_card(
                chat_id, message_id, engagement_result_text(epayload.get("action") or action, note), None
            )
            return
        await self._edit_card(
            chat_id,
            message_id,
            engagement_result_text(
                (epayload.get("action") if isinstance(epayload.get("action"), dict) else action),
                f"⚠️ Approved but execution failed: {_err(epayload)}",
            ),
            None,
        )

    async def _handle_edit(
        self,
        engagement_id: int,
        *,
        chat_id: str,
        user_id: str,
        message_id: str | None,
        answer: AnswerFn | None,
    ) -> None:
        code, payload = await self._run(
            "list", "--account", self.account, "--status", "pending_approval", "--limit", "100"
        )
        rows = (payload.get("actions") or []) if code == 0 and payload.get("ok") else []
        target = next((row for row in rows if row.get("id") == engagement_id), None)
        if target is None:
            await self._answer(answer, "Already resolved — no longer pending.")
            return
        self.store.create(
            chat_id=chat_id,
            user_id=user_id,
            engagement_id=engagement_id,
            account_key=self.account,
            card_message_id=message_id,
        )
        await self._answer(answer, "✏️ Send the replacement reply.")
        await self._send(
            chat_id,
            "\n".join(
                [
                    f"Send the replacement reply for engagement #{engagement_id}.",
                    "",
                    "Current reply:",
                    str(target.get("proposed_text") or "").strip() or "(empty)",
                    "",
                    "Your next message here becomes the new proposed reply (still pending approval).",
                    "Send 'cancel' to abort.",
                ]
            ),
        )

    async def _handle_reject(
        self,
        engagement_id: int,
        *,
        chat_id: str,
        user_id: str,
        message_id: str | None,
        answer: AnswerFn | None,
    ) -> None:
        ref = f"tg:{chat_id}:{message_id}" if message_id else f"tg:{chat_id}"
        code, payload = await self._run(
            "reject", "--account", self.account, "--id", str(engagement_id), "--approval-ref", ref
        )
        if code != 0 or not payload.get("ok"):
            if _is_not_pending(payload):
                await self._answer(answer, "Already resolved — no longer pending.")
            else:
                await self._answer(answer, f"❌ Reject failed: {_err(payload)}")
            return
        action = payload.get("action") or {"id": engagement_id}
        await self._answer(answer, "❌ Rejected.")
        await self._edit_card(
            chat_id,
            message_id,
            engagement_result_text(action, "❌ Rejected / skipped. This item will not be shown as pending again."),
            None,
        )
