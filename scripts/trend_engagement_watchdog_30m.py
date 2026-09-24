#!/usr/bin/env python3
"""Workflow A watchdog: draft trend candidates -> Telegram approval cards.

Runs the threads-operator trend-engagement draft command for the configured
account. The CLI now owns Telegram delivery: each newly drafted candidate
gets exactly one approval card, and only after a confirmed send does the
store transition to ``pending_approval`` with ``approval_sent_at`` set.

Legacy rows stuck in ``pending_approval`` with ``approval_sent_at IS NULL``
(cards never delivered under the old broken flow) are also recovered: the
watchdog sends their card once via ``trend-engagement telegram-send``, which
stamps ``approval_sent_at = now()`` after confirmed delivery. The stamp is
CAS-guarded (``approval_sent_at IS NULL``) so repeat watchdog runs can never
emit duplicate cards.

Schedule: every 30 minutes (cron-managed). Nothing in this script publishes;
approval enqueues into the normal publish queue which the existing
publish-worker owns.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = os.environ.get("THREADS_OPERATOR_REPO", str(Path(__file__).resolve().parent.parent))
CLI = [".venv/bin/threads-operator", "trend-engagement"]
ACCOUNT = os.environ.get("THREADS_ACCOUNT", "syaqir")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def _run_cli(args: list[str], *, cli_prefix: list[str] | None = None) -> dict:
    proc = subprocess.run(
        [*(cli_prefix or CLI), *args, "--account", ACCOUNT],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = proc.stdout.strip()
    try:
        return json.loads(out) if out else {}
    except json.JSONDecodeError:
        print(f"non-JSON CLI output rc={proc.returncode}: {proc.stderr[:300]}", file=sys.stderr)
        return {}


TREND_CLI = [".venv/bin/threads-operator", "trend"]


def main() -> int:
    if not BOT_TOKEN or not CHAT_ID:
        print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; skipping delivery", file=sys.stderr)
        return 1

    # 1. New drafts -> pending_approval (CLI sends Telegram first, then stamps)
    payload = _run_cli(["draft", "--limit", "5"])
    if not payload:
        return 1
    if not payload.get("ok"):
        print(f"draft run failed: {payload}", file=sys.stderr)
        return 1

    # 2. Recover legacy rows: pending_approval with approval_sent_at IS NULL
    legacy_sent = 0
    legacy_failed = 0
    pending = _run_cli(["list", "--status", "pending_approval", "--limit", "50"])
    if pending.get("ok"):
        for row in pending.get("candidates") or []:
            cid = row.get("id")
            if not cid:
                continue
            show = _run_cli(["show", "--id", str(cid)], cli_prefix=TREND_CLI)
            candidate = (show or {}).get("candidate") or {}
            if not candidate or candidate.get("approval_sent_at"):
                continue
            send = _run_cli(["telegram-send", "--id", str(cid)])
            if send.get("ok") and not send.get("skipped"):
                legacy_sent += 1
            elif not send.get("ok"):
                legacy_failed += 1

    print(json.dumps({
        "drafted": payload.get("drafted_count", 0),
        "skipped": payload.get("skipped_count", 0),
        "errors": payload.get("error_count", 0),
        "legacy_cards_sent": legacy_sent,
        "legacy_cards_failed": legacy_failed,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
