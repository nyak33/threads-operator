#!/usr/bin/env python3
"""Workflow A approval timeout watchdog: pending_approval -> backlog.

Runs once per hour (cron-managed). Moves only Workflow A standalone-post drafts
that have been pending_approval for more than 24 hours into backlog.

Idempotent: each row is CAS-updated from pending_approval -> backlog, so a
rerun cannot double-move or enqueue anything. No publishing happens here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = os.environ.get("THREADS_OPERATOR_HOME", str(Path(__file__).resolve().parent.parent))
CLI = [".venv/bin/threads-operator", "trend-engagement"]
ACCOUNT = os.environ.get("THREADS_ACCOUNT", "syaqir")


def main() -> int:
    proc = subprocess.run(
        [*CLI, "timeout", "--account", ACCOUNT, "--older-than-hours", "24"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = proc.stdout.strip()
    try:
        payload = json.loads(out) if out else {}
    except json.JSONDecodeError:
        print(
            f"non-JSON CLI output rc={proc.returncode}: {proc.stderr[:300]}",
            file=sys.stderr,
        )
        return 1
    if not payload.get("ok"):
        print(f"timeout run failed: {payload}", file=sys.stderr)
        return 1
    print(json.dumps({
        "moved_to_backlog": payload.get("moved_to_backlog", 0),
        "errors": len(payload.get("errors") or []),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
