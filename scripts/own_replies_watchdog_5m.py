#!/usr/bin/env python3
"""Workflow B watchdog: discover replies under our own posts -> approval cards.

Every 5 minutes:
1. `own-replies scan --propose` — Graph API discovery of new inbound replies,
   dedup by reply id, generate one persona draft per new reply, move each to
   pending_approval. Nothing publishes here.
2. Send a Telegram approval card per newly proposed reply.
3. `own-replies publish-approved` — publish ONLY rows already approved via
   Telegram, with claim-first CAS and transient/permanent error separation.

Nothing in this script uses browser automation or replies to external posts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = os.environ.get("THREADS_OPERATOR_REPO", str(Path(__file__).resolve().parent.parent))
CLI = [".venv/bin/threads-operator", "own-replies"]
ACCOUNT = os.environ.get("THREADS_ACCOUNT", "syaqir")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_card(text: str, row_id: int) -> None:
    if not BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN not set; card not sent", file=sys.stderr)
        return
    if not CHAT_ID:
        print("TELEGRAM_CHAT_ID not set; card not sent", file=sys.stderr)
        return
    keyboard = {
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


def run_cli(args: list[str], timeout: int = 240) -> tuple[int, dict]:
    proc = subprocess.run(
        [*CLI, *args, "--account", ACCOUNT],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = proc.stdout.strip()
    try:
        return proc.returncode, json.loads(out) if out else {}
    except json.JSONDecodeError:
        return proc.returncode or 1, {"ok": False, "error": proc.stderr[:300]}


def main() -> int:
    rc = 0

    # 1) discover + draft
    code, payload = run_cli(["scan", "--propose", "--limit", "10"])
    if not payload.get("ok"):
        print(f"scan failed: {payload}", file=sys.stderr)
        rc = 1
    else:
        for item in payload.get("proposed") or []:
            card = item.get("card")
            rid = item.get("id")
            if card and rid:
                send_card(card, int(rid))

    # 2) publish whatever Telegram already approved (gate stays in Supabase)
    code, pub = run_cli(["publish-approved"])
    if code != 0 and pub.get("error"):
        # disabled-gate errors are routine when THREADS_ENGAGEMENT_ENABLED=false
        print(f"publish-approved: {pub.get('error')}", file=sys.stderr)
    elif pub.get("results"):
        for r in pub["results"]:
            if r.get("status") == "failed":
                send_card(
                    f"⚠️ Own-reply publish FAILED (row #{r.get('id')})\n"
                    f"Error: {r.get('error', 'unknown')[:300]}\n"
                    f"Permanent: {r.get('permanent')}",
                    int(r.get("id") or 0),
                )

    print(json.dumps({
        "new_replies": payload.get("new_replies", 0) if payload else 0,
        "proposed": payload.get("proposed_count", 0) if payload else 0,
        "published": pub.get("posted", 0) if pub else 0,
        "failed": pub.get("failed", 0) if pub else 0,
    }))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
