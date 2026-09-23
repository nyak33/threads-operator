"""Telegram alerts for stalled / failed publish-queue rows.

Sends through the existing operator bot (``TELEGRAM_BOT_TOKEN`` from
``HERMES_ENV_FILE``, default ``~/.hermes/.env``) to the configured home
channel. Alerts are deduplicated via a small state file so the same row is
not re-alerted every 5-minute cron tick: a row re-alerts only when its
attempt count advances.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .supabase_store import SupabaseStore

_MYTZ = timezone(timedelta(hours=8))  # Asia/Kuala_Lumpur (no DST)

# Machine-independent defaults: Hermes env file lives under $HOME (overridable
# via HERMES_ENV_FILE); operator state lives under the operator home
# (THREADS_OPERATOR_HOME, matching account_config.resolve_home).
_DEFAULT_ENV_FILE = Path(
    os.environ.get("HERMES_ENV_FILE")
    or Path.home() / ".hermes" / ".env")
_DEFAULT_STATE_FILE = Path(
    os.environ.get("THREADS_OPERATOR_HOME")
    or Path.home() / ".threads-operator") / ".publish_alert_state.json"
# Re-alert only after the row's attempt count has advanced by at least this many
# since the last alert we sent for it.
_ATTEMPT_STEP = 1


def _load_env_value(key: str, env_file: Path = _DEFAULT_ENV_FILE) -> str | None:
    if os.environ.get(key):
        return os.environ[key]
    try:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def _read_state(state_file: Path) -> dict[str, Any]:
    try:
        return json.loads(state_file.read_text())
    except (OSError, ValueError):
        return {}


def _write_state(state_file: Path, state: dict[str, Any]) -> None:
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state))
    except OSError:
        pass


def _myt(iso_ts: str | None) -> str:
    if not iso_ts:
        return "unknown"
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_MYTZ).strftime("%Y-%m-%d %I:%M %p MYT")
    except Exception:
        return str(iso_ts)


def send_publish_alert(
    store: SupabaseStore,
    table: str,
    row_id: int | str,
    result: dict[str, Any],
    attempts: int,
    last_error: str,
    *,
    reason: str,
    chat_id: str | None = None,
    bot_token: str | None = None,
    state_file: Path = _DEFAULT_STATE_FILE,
    dry_run: bool = False,
) -> bool:
    """Send a deduplicated Telegram alert for a stalled/failed queue row.

    Returns True when an alert message was actually sent. Deduplication: the
    same (table,row_id) re-sends only when ``attempts`` has advanced past the
    last alerted attempt count, so a stuck row is not re-alerted every tick.
    """
    key = f"{table}:{row_id}"
    state = _read_state(state_file)
    last = state.get(key) or {}
    last_attempts = int(last.get("attempts", -1))
    if last and attempts < last_attempts + _ATTEMPT_STEP:
        return False  # already alerted for this attempt count

    # Enrich with queue metadata (campaign, scheduled time).
    campaign = result.get("campaign_code")
    scheduled_at = result.get("scheduled_at")
    status = result.get("status", "failed")
    row: dict[str, Any] | None = None
    try:
        row = store.fetch_queue_row(table, row_id)
        if row:
            campaign = campaign or row.get("campaign_code")
            scheduled_at = scheduled_at or row.get("scheduled_at")
            status = row.get("status", status)
    except Exception:
        pass

    reason_label = {
        "max_attempts": "max retry attempts reached",
        "manual_review": "non-retryable content rejection",
        "stalled": "stalled past schedule",
        "stale_reclaim": "stale posting row reclaimed; resume failed",
        "duplicate_risk": "duplicate-risk ambiguity — row isolated, not retried",
    }.get(reason, reason)

    lines = [
        "⚠️ *Threads publish queue alert*",
        f"• Queue ID: `{row_id}`",
        f"• Campaign: `{campaign or '—'}`",
        f"• Scheduled: {_myt(scheduled_at)}",
        f"• Attempts: `{attempts}`",
        f"• Status: `{status}`",
        f"• Reason: {reason_label}",
    ]
    if row:
        root_id = row.get("threads_main_post_id") or result.get("main_post_id")
        done = result.get("reply_ids") or row.get("threads_reply_ids") or []
        expected = row.get("reply_texts") or []
        if root_id:
            lines.append(f"• Root: `{root_id}`")
        lines.append(f"• Replies: {len(done)}/{len(expected)} completed")
    lines.append(f"• Last error: {last_error[:600]}")
    lines.append(
        "• Action taken: row isolated automatically; other queue rows continue"
    )
    text = "\n".join(lines)
    token = bot_token or _load_env_value("TELEGRAM_BOT_TOKEN")
    # No hardcoded operator chat id: alerts go to the explicit chat, the
    # configured home channel, or nowhere (caller sees False). On this VPS
    # TELEGRAM_HOME_CHANNEL is set in ~/.hermes/.env, so behavior is unchanged.
    chat = chat_id or _load_env_value("TELEGRAM_HOME_CHANNEL") or _load_env_value("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    if dry_run:
        return True

    import httpx

    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "Markdown"},
            timeout=30,
        )
        if resp.status_code == 200:
            state[key] = {"attempts": attempts, "sent_at": datetime.now(timezone.utc).isoformat()}
            _write_state(state_file, state)
            return True
    except Exception:
        return False
    return False
