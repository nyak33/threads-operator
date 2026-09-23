#!/usr/bin/env python3
"""Workflow A watchdog: draft trend candidates -> Telegram approval cards.

Runs the threads-operator trend-engagement draft command for the configured
account, then sends one Telegram approval card per newly drafted candidate.
Approval buttons carry only `trendeng:<action>:<id>` — never content or credentials.

Schedule: every 30 minutes (cron-managed). Nothing in this script publishes;
approval enqueues into the normal publish queue which the existing
publish-worker owns.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = os.environ.get("THREADS_OPERATOR_REPO", str(Path(__file__).resolve().parent.parent))
CLI = [".venv/bin/threads-operator", "trend-engagement"]
ACCOUNT = os.environ.get("THREADS_ACCOUNT", "syaqir")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_card(text: str, candidate_id: int) -> None:
    if not BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN not set; card not sent", file=sys.stderr)
        return
    if not CHAT_ID:
        print("TELEGRAM_CHAT_ID not set; card not sent", file=sys.stderr)
        return
    keyboard = {
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
    body = json.dumps({
        "chat_id": CHAT_ID,
        "text": text[:4000],
        "reply_markup": keyboard,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode() or "{}")
        if not result.get("ok"):
            print(f"telegram send failed: {result}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"telegram send error: {exc}", file=sys.stderr)


def main() -> int:
    proc = subprocess.run(
        [*CLI, "draft", "--account", ACCOUNT, "--limit", "5"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = proc.stdout.strip()
    try:
        payload = json.loads(out) if out else {}
    except json.JSONDecodeError:
        print(f"non-JSON CLI output rc={proc.returncode}: {proc.stderr[:300]}", file=sys.stderr)
        return 1
    if not payload.get("ok"):
        print(f"draft run failed: {payload}", file=sys.stderr)
        return 1
    for item in payload.get("drafted") or []:
        card = item.get("card")
        cid = item.get("id")
        if card and cid:
            send_card(card, int(cid))
    print(json.dumps({
        "drafted": payload.get("drafted_count", 0),
        "skipped": payload.get("skipped_count", 0),
        "errors": payload.get("error_count", 0),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
