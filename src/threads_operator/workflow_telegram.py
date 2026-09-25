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
import os
from datetime import datetime, timezone
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
from .scheduling import MYT, ScheduleParseError, parse_custom_time

logger = logging.getLogger(__name__)

TRENDENG_PREFIX = "trendeng:"
TRENDENG_ACTIONS = ("approve", "edit", "reject", "skip", "use")

OWNREPLY_PREFIX = "ownreply:"
OWNREPLY_ACTIONS = ("approve", "edit", "reject", "ignore")

BACKLOG_PREFIX = "backlog:"
BACKLOG_ACTIONS = ("use", "edit", "refresh", "discard", "next")

# Smart scheduling (Task #3) — second-step "when to publish" actions.
TRENDSCHED_PREFIX = "trendsched:"
TRENDSCHED_ACTIONS = ("best", "now", "choose", "confirm", "cancel")

# Task 2C — DM approval (Threads DMs gated by Telegram approval).
DMOPP_PREFIX = "dmopp:"
DMOPP_ACTIONS = ("approve", "edit", "reject")

# Session "kind" markers stored in the PendingEditStore card_message_id field so
# the generic text interceptor can route them to the right handler.
SCHED_INPUT_KIND = "trendsched:input"
SCHED_CONFIRM_KIND = "trendsched:confirm"

CANCEL_WORDS = {"cancel"}
MAX_CHARS = 500

_SCHED_MODE_LABELS = {
    "historical": "⭐️ Best Time",
    "fallback": "⭐️ Best Time",
    "now": "⚡️ Post Now",
    "custom": "🕐 Custom Time",
}


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


def backlog_keyboard(candidate_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Use", "callback_data": f"backlog:use:{candidate_id}"},
                {"text": "✏️ Edit", "callback_data": f"backlog:edit:{candidate_id}"},
                {"text": "🔄 Refresh", "callback_data": f"backlog:refresh:{candidate_id}"},
            ],
            [
                {"text": "🗑️ Discard", "callback_data": f"backlog:discard:{candidate_id}"},
                {"text": "➡️ Next", "callback_data": f"backlog:next:{candidate_id}"},
            ],
        ]
    }


def backlog_card_text(item: dict[str, Any]) -> str:
    """Render one backlog item for Telegram. No credentials or secrets."""
    wa = (item.get("raw_metadata") or {}).get("workflow_a") or {}
    draft = str(wa.get("draft_text") or "").strip()
    preview = draft if len(draft) <= 200 else draft[:197] + "..."
    topic = item.get("topic") or wa.get("topic") or "n/a"
    username = item.get("source_username") or "unknown"
    permalink = item.get("source_permalink") or "n/a"
    timed_out = item.get("timed_out_at") or item.get("updated_at") or "n/a"
    lines = [
        "🗄️ CONTENT BACKLOG",
        "",
        f"Candidate #{item.get('id')}",
        f"Topic: {topic}",
        f"Age: {timed_out}",
        f"Source: @{username}",
        f"URL: {permalink}",
        "",
        f"Draft ({len(draft)} chars):\n{preview}",
    ]
    return "\n".join(lines)


def dm_approval_keyboard(opportunity_id: int) -> dict[str, Any]:
    """Approve/Edit/Reject keyboard for one DM opportunity card."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"dmopp:approve:{opportunity_id}"},
                {"text": "✏️ Edit", "callback_data": f"dmopp:edit:{opportunity_id}"},
            ],
            [
                {"text": "❌ Reject", "callback_data": f"dmopp:reject:{opportunity_id}"},
            ],
        ]
    }


def dm_approval_card_text(opp: dict[str, Any]) -> str:
    """Approval card body for one DM opportunity. No credentials, no JSON dump —
    just enough context for a human to decide."""
    username = opp.get("from_username") or "unknown"
    root = (opp.get("root_post_text") or "(original post text unavailable)").strip()
    root_excerpt = root if len(root) <= 200 else root[:197] + "..."
    reply = (opp.get("reply_text") or "(their reply text unavailable)").strip()
    reply_excerpt = reply if len(reply) <= 280 else reply[:277] + "..."
    intent = opp.get("intent") or "(unknown)"
    lead = opp.get("lead_score")
    lead_str = f"{float(lead):.2f}" if isinstance(lead, (int, float)) else "n/a"
    cta = (opp.get("cta_matched") or "").strip()
    draft = (opp.get("dm_draft_text") or "(no draft)").strip()

    lines = [
        "📩 DM APPROVAL",
        "",
        f"Opportunity #{opp.get('id')}  •  account @{opp.get('account_key')}",
        f"To: @{username}",
        "",
        f"Their reply:\n{reply_excerpt}",
        "",
        f"Intent: {intent}  •  lead score {lead_str}",
    ]
    if cta:
        lines.append(f"CTA matched: {cta}")
    lines += [
        "",
        f"Root post: {root_excerpt}",
        "",
        f"Proposed DM ({len(draft)} chars):\n{draft}",
    ]
    return "\n".join(lines)


def trendsched_keyboard(candidate_id: int) -> dict[str, Any]:
    """Second-step card shown after content approval: *when* to publish."""
    return {
        "inline_keyboard": [
            [{"text": "⭐️ Use Best Time", "callback_data": f"trendsched:best:{candidate_id}"}],
            [{"text": "⚡️ Post Now", "callback_data": f"trendsched:now:{candidate_id}"}],
            [{"text": "🕐 Choose Time", "callback_data": f"trendsched:choose:{candidate_id}"}],
            [{"text": "✖️ Cancel", "callback_data": f"trendsched:cancel:{candidate_id}"}],
        ]
    }


def trendsched_confirm_keyboard(candidate_id: int) -> dict[str, Any]:
    """Confirmation card for a custom-entered time."""
    return {
        "inline_keyboard": [
            [{"text": "✅ Confirm", "callback_data": f"trendsched:confirm:{candidate_id}"}],
            [{"text": "🕐 Change Time", "callback_data": f"trendsched:choose:{candidate_id}"}],
            [{"text": "✖️ Cancel", "callback_data": f"trendsched:cancel:{candidate_id}"}],
        ]
    }


def _fmt_myt(utc_iso: str | None) -> str:
    """Render a UTC ISO timestamp as '25 Sep 2026, 9:00 PM MYT'."""
    if not utc_iso:
        return "n/a"
    try:
        ts = datetime.fromisoformat(str(utc_iso).replace("Z", "+00:00"))
    except ValueError:
        return str(utc_iso)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    local = ts.astimezone(MYT)
    return local.strftime("%d %b %Y, %-I:%M %p MYT")


def trendsched_card_text(candidate_id: int, draft_preview: str) -> str:
    preview = draft_preview if len(draft_preview) <= 200 else draft_preview[:197] + "..."
    return "\n".join([
        "✅ Content approved for publication.",
        "",
        f"Candidate #{candidate_id}",
        f"Draft ({len(draft_preview)} chars):\n{preview}",
        "",
        "🕐 When should this post be published?",
    ])


def trendsched_confirm_card_text(candidate_id: int, when_utc_iso: str, draft_preview: str) -> str:
    preview = draft_preview if len(draft_preview) <= 200 else draft_preview[:197] + "..."
    return "\n".join([
        "🕐 Confirm schedule",
        "",
        f"Candidate #{candidate_id}",
        f"Post:\n{preview}",
        "",
        f"Schedule:\n{_fmt_myt(when_utc_iso)}",
        "",
        "Confirm to schedule this post.",
    ])


def trendsched_success_message(payload: dict[str, Any]) -> str:
    """Final confirmation chat message after a scheduling DB commit.

    Renders ONLY from the persisted queue row returned by the CLI
    (``queue_row``), never from pre-commit session values. Times are shown in
    MYT. ``scheduled_at`` is omitted for ⚡️ Post Now (due immediately).
    """
    cid = payload.get("id")
    row = payload.get("queue_row") or {}
    source = str(payload.get("schedule_source") or "")
    mode = _SCHED_MODE_LABELS.get(source, "✅ Scheduled")
    post = str(row.get("main_post_text") or "").strip()
    preview = post if len(post) <= 200 else post[:197] + "..."
    qid = row.get("id") or payload.get("queue_id")
    if source == "now":
        lines = [
            "✅ Post approved for publishing.",
            "",
            f"Candidate #{cid}",
            f"Post:\n{preview}",
            f"Mode: {mode}",
            f"Queue ID: #{qid}",
            "Status: Approved — due now",
            "",
            "The normal publisher will publish it on the next worker run.",
        ]
    else:
        lines = [
            "✅ Post scheduled.",
            "",
            f"Candidate #{cid}",
            f"Post:\n{preview}",
            f"Scheduled: {_fmt_myt(row.get('scheduled_at') or payload.get('scheduled_at'))}",
            f"Mode: {mode}",
            f"Queue ID: #{qid}",
            "Status: Approved — waiting for publisher",
            "",
            "The post will be published automatically at the scheduled time.",
        ]
    return "\n".join(lines)


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
        edit: EditFn | None = None,
        cli_argv: list[str] | None = None,
        cwd: str | None = None,
        edit_store: PendingEditStore | None = None,
    ) -> None:
        self.account = account
        self._send = send
        self._edit = edit or edit_message
        self.cli_argv = cli_argv or list(DEFAULT_CLI)
        self.cwd = cwd if cwd is not None else self._default_cwd()
        self._verify_cli_exists()
        if edit_store is None:
            default_path = Path.home() / ".threads-operator" / "engagement_edit_sessions.json"
            edit_store = PendingEditStore(default_path)
        self.store = edit_store

    @staticmethod
    def _default_cwd() -> str | None:
        """Resolve the repo root (dir containing pyproject.toml) from this file.

        The gateway instantiates these dispatchers without a cwd; the CLI is a
        relative path (.venv/bin/threads-operator) that only resolves from the
        repo root. Mirrors EngagementTelegramBridge._default_cwd.
        """
        here = Path(__file__).resolve()
        for parent in here.parents:
            if (parent / "pyproject.toml").exists():
                return str(parent)
        return None

    def _verify_cli_exists(self) -> None:
        """Fail fast with a clear diagnostic when the CLI binary is missing."""
        argv0 = self.cli_argv[0] if self.cli_argv else ""
        if not argv0 or os.path.isabs(argv0):
            return
        if self.cwd is None:
            logger.warning(
                "workflow dispatcher: no repo root found (pyproject.toml) and "
                "cli_argv[0]=%r is relative — subprocess will fail with "
                "FileNotFoundError from the caller's cwd",
                argv0,
            )
            return
        candidate = Path(self.cwd) / argv0
        if not candidate.exists():
            logger.warning(
                "workflow dispatcher: CLI binary %s does not exist under "
                "resolved cwd %s — callbacks will fail (argv=%r)",
                argv0,
                self.cwd,
                self.cli_argv,
            )

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
            if "not pending" in err.lower() or "not in an open state" in err.lower() or "not backlog" in err.lower():
                self.store.remove(session["token"])
                await self._send(text=f"⚠️ #{row_id} is no longer awaiting edits; edit session closed.")
            else:
                await self._send(text=f"❌ Edit failed for #{row_id}: {err} — session still open; send new text or 'cancel'.")
            return True
        self.store.remove(session["token"])
        await self._send(text=f"✏️ #{row_id} updated — still in backlog.")
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
        # After content approval, ask *when* to publish instead of enqueueing a
        # draft. The queue row is only created (approved) once a scheduling
        # option is chosen. Other actions keep the base behavior.
        if action != "approve":
            await super()._decide(row_id, action, chat_id=chat_id, message_id=message_id, answer=answer)
            return
        ref = f"tg:{chat_id}:{message_id}" if message_id else f"tg:{chat_id}"
        code, payload = await _run_cli(
            self.cli_argv, self.group,
            ["approve", "--account", self.account, "--id", str(row_id), "--approval-ref", ref],
            cwd=self.cwd,
        )
        if code != 0 or not payload.get("ok"):
            await self._answer(answer, f"❌ Approve failed: {_err(payload)}")
            return
        if payload.get("already_queued"):
            await self._answer(answer, f"✅ #{row_id}: already scheduled (queue #{payload.get('queue_id')}).")
            return
        # Content is approved (or already approved) — prompt for scheduling.
        preview = str(payload.get("draft_preview") or "").strip() or f"Candidate #{row_id}"
        await self._answer(answer, f"✅ #{row_id}: content approved.")
        await self._send(
            str(chat_id),
            trendsched_card_text(row_id, preview),
            markup=trendsched_keyboard(row_id),
        )


class OwnReplyDispatcher(_BaseWorkflowDispatcher):
    """Workflow B: ownreply: callbacks -> own-replies CLI group."""

    group = "own-replies"
    prefix = OWNREPLY_PREFIX
    actions = OWNREPLY_ACTIONS
    edit_prompt = "Send the new reply text, or 'cancel'."


class DMDispatcher(_BaseWorkflowDispatcher):
    """Task 2C: dmopp: callbacks -> own-replies dm CLI subcommands.

    Stops at ``approved`` (or a terminal rejection) — it never sends a DM.
    The base class already owns: callback parsing, the PendingEditStore edit
    session (30-min expiry, restart-safe), empty/cancel handling, and the
    CLI subprocess boundary. This subclass only customises messaging and the
    post-edit card refresh.
    """

    group = "own-replies"
    prefix = DMOPP_PREFIX
    actions = DMOPP_ACTIONS
    edit_prompt = (
        "Send the replacement DM text, or 'cancel'. "
        "Approving is a separate step — editing never sends."
    )

    async def _start_edit(
        self, row_id: int, *, chat_id: str, user_id: str, answer: AnswerFn | None
    ) -> None:
        # Tag the session so the card refresh can edit the original card
        # message in place when the user submits replacement text.
        session = self.store.create(
            chat_id=chat_id, user_id=user_id, engagement_id=row_id,
            account_key=self.account,
        )
        session["card_message_id"] = session.get("card_message_id")
        await self._answer(
            answer,
            f"✏️ Edit DM for opportunity #{row_id}. {self.edit_prompt}",
        )

    async def _decide(
        self, row_id: int, action: str, *, chat_id: str, message_id: str | None, answer: AnswerFn | None
    ) -> None:
        ref = f"tg:{chat_id}:{message_id}" if message_id else f"tg:{chat_id}"
        code, payload = await _run_cli(
            self.cli_argv, self.group,
            ["dm", action, "--account", self.account, "--id", str(row_id), "--approval-ref", ref],
            cwd=self.cwd,
        )
        if code != 0 or not payload.get("ok"):
            err = _err(payload)
            # Stale / replayed callback: already decided or expired. Answer
            # informatively; never surface as a hard failure to the user.
            if payload.get("already_decided"):
                await self._answer(
                    answer,
                    f"ℹ️ #{row_id} already {payload.get('status', 'decided')} — nothing changed.",
                )
                return
            if "expired" in err.lower():
                await self._answer(
                    answer,
                    f"⏰ Opportunity #{row_id} has expired and can no longer be {action}d.",
                )
                return
            await self._answer(answer, f"❌ {action.title()} failed: {err}")
            return
        status = payload.get("status", action)
        if action == "approve":
            note = (
                f"✅ DM opportunity #{row_id} approved.\n"
                "It will be sent by the DM sender (Task 2D) — no DM has been sent yet."
            )
        elif action == "reject":
            note = f"❌ DM opportunity #{row_id} rejected."
        else:
            note = f"✅ #{row_id}: {action} recorded ({status})."
        await self._answer(answer, note)
        # Strip the buttons off the decided card if we can edit it in place.
        if self._edit is not None and message_id:
            try:
                await self._edit(str(chat_id), str(message_id), note, None)
            except Exception:  # noqa: BLE001
                logger.warning("dm card edit-after-decide failed", exc_info=True)

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
            await self._send(text=f"✏️ DM edit cancelled for #{row_id}. Draft unchanged.")
            return True
        if not body:
            await self._send(
                text="Replacement DM text is empty — send the new text, or 'cancel'."
            )
            return True
        code, payload = await _run_cli(
            self.cli_argv, self.group,
            ["dm", "edit", "--account", self.account, "--id", str(row_id), "--text", body],
            cwd=self.cwd,
        )
        if code != 0 or not payload.get("ok"):
            err = _err(payload)
            lower = err.lower()
            if payload.get("already_decided") or "not awaiting" in lower or "expired" in lower:
                self.store.remove(session["token"])
                await self._send(
                    text=f"⚠️ #{row_id} is no longer awaiting edits ({err}); edit session closed."
                )
            else:
                await self._send(
                    text=f"❌ DM edit failed for #{row_id}: {err} — session still open; send new text or 'cancel'."
                )
            return True
        self.store.remove(session["token"])
        await self._send(
            text=f"✏️ DM draft for #{row_id} updated — still awaiting approval. Review the refreshed card."
        )
        # Send the refreshed approval card with the edited draft.
        refreshed = (payload.get("dm_draft_text") or body)
        card = payload.get("card_text") or f"Proposed DM ({len(refreshed)} chars):\n{refreshed}"
        await self._send(
            str(chat_id),
            card,
            markup=dm_approval_keyboard(row_id),
        )
        return True


class TrendBacklogDispatcher(_BaseWorkflowDispatcher):
    """Workflow A backlog: backlog: callbacks -> trend-engagement CLI group."""

    group = "trend-engagement"
    prefix = BACKLOG_PREFIX
    actions = BACKLOG_ACTIONS
    edit_prompt = "Send the new draft text for this backlog post, or 'cancel'."

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
        if action == "next":
            # Re-render the backlog list starting after this item.
            await self._send_backlog_list(chat_id=str(chat_id), after_id=row_id)
            await self._answer(answer, "Next backlog item.")
            return True
        await self._decide(row_id, action, chat_id=str(chat_id), message_id=message_id, answer=answer)
        return True

    async def _decide(
        self, row_id: int, action: str, *, chat_id: str, message_id: str | None, answer: AnswerFn | None
    ) -> None:
        code, payload = await _run_cli(
            self.cli_argv, self.group,
            [action, "--account", self.account, "--id", str(row_id)],
            cwd=self.cwd,
        )
        if code != 0 or not payload.get("ok"):
            err = _err(payload)
            if "already_queued" in str(payload):
                await self._answer(answer, "✅ Already enqueued from this backlog item.")
                return
            if "expired" in err.lower() or "moved to content backlog" in err.lower():
                await self._answer(answer, "This approval expired and was moved to Content Backlog.")
                return
            await self._answer(answer, f"❌ {action.title()} failed: {err}")
            return

        if action == "use" and payload.get("needs_scheduling"):
            # Content approved from backlog — ask when to publish (Task #3).
            await self._answer(answer, "✅ Content approved.")
            preview = str(payload.get("draft_preview") or "").strip() or f"Candidate #{row_id}"
            await self._send(
                str(chat_id),
                trendsched_card_text(row_id, preview),
                markup=trendsched_keyboard(row_id),
            )
            return

        if action == "use":
            note = f"✅ Enqueued (queue #{payload.get('queue_id')}) — topic preserved: {payload.get('topic')}."
        elif action == "refresh":
            note = "🔄 Draft refreshed — still in backlog for review."
        elif action == "discard":
            note = "🗑️ Discarded. The content is kept but no longer shown as active backlog."
        else:
            note = f"✅ {action.title()} recorded."

        await self._answer(answer, note)

        # Refresh the card inline if possible, removing the action buttons.
        if action in ("use", "discard") and self._edit is not None and message_id:
            try:
                await self._edit(
                    str(chat_id), str(message_id),
                    f"{note}\n\n(Item #{row_id})",
                    None,
                )
            except Exception:  # noqa: BLE001
                logger.warning("backlog card edit failed", exc_info=True)

    async def handle_command(self, chat_id: str) -> None:
        """Entry point for the Telegram ``/content-backlog`` command."""
        await self._send_backlog_list(chat_id=str(chat_id))

    async def _send_backlog_list(
        self, chat_id: str, after_id: int | None = None
    ) -> None:
        """Fetch and render the next backlog item after ``after_id``."""
        code, payload = await _run_cli(
            self.cli_argv, self.group,
            ["backlog", "--account", self.account, "--limit", "20"],
            cwd=self.cwd,
        )
        if code != 0 or not payload.get("ok"):
            await self._send(
                chat_id,
                f"❌ Could not load content backlog: {_err(payload)}",
            )
            return
        items = payload.get("items") or []
        if after_id is not None:
            items = [item for item in items if item.get("id") != after_id]
        if not items:
            await self._send(chat_id, "🗄️ Content Backlog is empty.")
            return
        item = items[0]
        await self._send(
            chat_id,
            backlog_card_text(item),
            markup=backlog_keyboard(int(item["id"])),
        )


# Aliases used by the Hermes Telegram gateway adapter (do not remove).
class TrendEngagementTelegramBridge(TrendEngagementDispatcher):
    """Gateway-compatible alias for Workflow A."""


class OwnReplyTelegramBridge(OwnReplyDispatcher):
    """Gateway-compatible alias for Workflow B."""


class DMTelegramBridge(DMDispatcher):
    """Gateway-compatible alias for Task 2C DM approval."""


class TrendBacklogTelegramBridge(TrendBacklogDispatcher):
    """Gateway-compatible alias for the Workflow A backlog UX."""


class TrendSchedDispatcher(_BaseWorkflowDispatcher):
    """Smart-scheduling step (Task #3): trendsched: callbacks.

    Shown *after* content approval. Every completed path produces an approved +
    scheduled publish-queue row via the trend-engagement CLI ``schedule-*``
    subcommands. Custom-time input is a temporary PendingEditStore session.
    """

    group = "trend-engagement"
    prefix = TRENDSCHED_PREFIX
    actions = TRENDSCHED_ACTIONS
    edit_prompt = ""  # unused; scheduling uses its own prompts

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
        action, cid = parsed
        if action in ("best", "now"):
            await self._run_schedule(cid, f"schedule-{action}", chat_id=str(chat_id), answer=answer)
        elif action == "choose":
            await self._start_choose(cid, chat_id=str(chat_id), user_id=str(user_id), answer=answer)
        elif action == "confirm":
            await self._run_confirm(cid, chat_id=str(chat_id), user_id=str(user_id), answer=answer)
        elif action == "cancel":
            await self._run_cancel(cid, chat_id=str(chat_id), user_id=str(user_id), answer=answer)
        return True

    async def _run_schedule(
        self, cid: int, sub: str, *, chat_id: str, answer: AnswerFn | None
    ) -> None:
        code, payload = await _run_cli(
            self.cli_argv, self.group,
            [sub, "--account", self.account, "--id", str(cid)],
            cwd=self.cwd,
        )
        if code != 0 or not payload.get("ok"):
            await self._answer(answer, f"❌ Scheduling failed: {_err(payload)}")
            return
        if payload.get("already_queued"):
            # Idempotent retry: the queue row was committed earlier. Acknowledge
            # via the toast but do NOT repeat the success confirmation message.
            await self._answer(
                answer,
                f"✅ #{cid}: already scheduled for "
                f"{_fmt_myt(payload.get('scheduled_at'))} "
                f"(queue #{payload.get('queue_id')}).",
            )
            return
        await self._answer(answer, "✅ Scheduling confirmed.")
        await self._send(str(chat_id), trendsched_success_message(payload))

    async def _start_choose(
        self, cid: int, *, chat_id: str, user_id: str, answer: AnswerFn | None
    ) -> None:
        self.store.create(
            chat_id=chat_id, user_id=user_id, engagement_id=cid,
            account_key=self.account, card_message_id=SCHED_INPUT_KIND,
        )
        await self._answer(
            answer,
            "🕐 Send the time to post (MYT), e.g.:\n"
            "• 9pm\n• tonight 9pm\n• tomorrow 8:30pm\n• 25 Sep 9pm\n• 2026-09-25 21:00\n\n"
            "or 'cancel'.",
        )

    async def _run_confirm(
        self, cid: int, *, chat_id: str, user_id: str, answer: AnswerFn | None
    ) -> None:
        session = self.store.find_for_sender(chat_id=chat_id, user_id=user_id)
        when = None
        if session and session.get("card_message_id", "").startswith(
            SCHED_CONFIRM_KIND + ":"
        ):
            when = session["card_message_id"][len(SCHED_CONFIRM_KIND) + 1:]
        if not when:
            await self._answer(
                answer,
                "⚠️ No pending time to confirm (session may have expired). "
                "Tap 🕐 Choose Time again.",
            )
            return
        code, payload = await _run_cli(
            self.cli_argv, self.group,
            ["schedule-time", "--account", self.account, "--id", str(cid), "--at", when],
            cwd=self.cwd,
        )
        if session:
            self.store.remove(session["token"])
        if code != 0 or not payload.get("ok"):
            await self._answer(answer, f"❌ Scheduling failed: {_err(payload)}")
            return
        if payload.get("already_queued"):
            await self._answer(
                answer,
                f"✅ #{cid}: already scheduled for "
                f"{_fmt_myt(payload.get('scheduled_at'))} "
                f"(queue #{payload.get('queue_id')}).",
            )
            return
        await self._answer(answer, "✅ Scheduling confirmed.")
        await self._send(str(chat_id), trendsched_success_message(payload))

    async def _run_cancel(
        self, cid: int, *, chat_id: str, user_id: str, answer: AnswerFn | None
    ) -> None:
        session = self.store.find_for_sender(chat_id=chat_id, user_id=user_id)
        if session:
            self.store.remove(session["token"])
        code, payload = await _run_cli(
            self.cli_argv, self.group,
            ["schedule-cancel", "--account", self.account, "--id", str(cid)],
            cwd=self.cwd,
        )
        if code != 0 or not payload.get("ok"):
            await self._answer(answer, f"❌ Cancel failed: {_err(payload)}")
            return
        await self._answer(
            answer,
            f"✖️ Scheduling cancelled for #{cid}. The content is approved but "
            "will NOT be posted. Re-approve (or use the backlog) to schedule it later.",
        )

    async def handle_text(
        self,
        *,
        chat_id: str,
        user_id: str,
        text: str,
        answer: AnswerFn | None = None,
    ) -> bool:
        """Consume a scheduling session message (custom-time input)."""
        session = self.store.find_for_sender(chat_id=str(chat_id), user_id=str(user_id))
        if session is None:
            return False
        kind = session.get("card_message_id") or ""
        if kind != SCHED_INPUT_KIND:
            return False  # not a scheduling-input session; leave for others
        cid = int(session["engagement_id"])
        body = (text or "").strip()
        if body.lower() in CANCEL_WORDS:
            self.store.remove(session["token"])
            await self._run_cancel(cid, chat_id=str(chat_id), user_id=str(user_id), answer=answer)
            return True
        try:
            when = parse_custom_time(body)
        except ScheduleParseError as exc:
            logger.info("schedule: custom-time parse failed for #%d: %s (%r)", cid, exc, body)
            await self._send(
                str(chat_id),
                f"⚠️ Couldn't understand that time ({exc}). Try e.g. '9pm', "
                "'tomorrow 8:30pm', '25 Sep 9pm', or 'cancel'.",
            )
            return True
        when_iso = when.isoformat()
        # Move the session to a confirm state carrying the parsed timestamp.
        self.store.remove(session["token"])
        self.store.create(
            chat_id=str(chat_id), user_id=str(user_id), engagement_id=cid,
            account_key=self.account, card_message_id=f"{SCHED_CONFIRM_KIND}:{when_iso}",
        )
        await self._send(
            str(chat_id),
            trendsched_confirm_card_text(cid, when_iso, f"Candidate #{cid}"),
            markup=trendsched_confirm_keyboard(cid),
        )
        return True


class TrendSchedTelegramBridge(TrendSchedDispatcher):
    """Gateway-compatible alias for the smart-scheduling step."""
