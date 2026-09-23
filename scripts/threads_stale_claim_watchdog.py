#!/usr/bin/env python3
"""Deterministic watchdog: detect threads_publish_queue rows stuck in 'posting'.

A row in 'posting' means an executor claimed it but the process died before
marking it posted/failed/requeued. Threads API has no idempotency key, so the
safe recovery is ALERT ONLY (no automatic requeue) — a human confirms on the
live thread whether the main post/replies landed, then requeues manually.
Exit 0 when healthy; exit 1 with a report when stuck rows exist.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running from repo or from Hermes scripts dir
_REPO = os.environ.get("THREADS_OPERATOR_HOME", str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(_REPO) / "src"))

from threads_operator.account_config import load_account_config
from threads_operator.operator_cli import _store

STALE_MINUTES = 30
ACCOUNT = os.environ.get("THREADS_ACCOUNT", "syaqir")

cfg = load_account_config(ACCOUNT)
store = _store(cfg)
resp = store.client.get(
    f"{store.base_url}/rest/v1/threads_publish_queue",
    headers=store._headers,
    params={"account_key": f"eq.{ACCOUNT}", "status": "eq.posting", "select": "*"},
)
resp.raise_for_status()
rows = resp.json()
now = datetime.now(timezone.utc)
stuck = []
for r in rows:
    upd = r.get("updated_at")
    if not upd:
        continue
    ts = datetime.fromisoformat(upd.replace("Z", "+00:00"))
    age = (now - ts).total_seconds() / 60
    if age > STALE_MINUTES:
        stuck.append((r["id"], r.get("campaign_code"), r.get("scheduled_at"),
                      r.get("threads_main_post_id"), round(age)))
if not stuck:
    # Silent when healthy (no_agent cron delivers nothing on empty stdout).
    sys.exit(0)
report = "STALE-CLAIM ALERT: %d row(s) stuck in 'posting' >%d min\n" % (len(stuck), STALE_MINUTES)
for sid, camp, sched, main_id, age in stuck:
    report += ("  id=%s campaign=%s sched=%s main_post=%s stuck_min=%d\n"
               % (sid, camp, sched, main_id or "NONE", age))
report += "Action: check live thread, then requeue manually (API has no idempotency key)."
print(report, end="")
sys.exit(1)
