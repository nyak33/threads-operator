#!/usr/bin/env bash
# ── collect_activity_follows.sh ───────────────────────────────
# Idempotent cron-safe runner for the Activity → Follows collector.
# Writes structured JSON to stdout; exit 0 = success, 1 = failure.
# Failure isolates from other cron jobs (insights / posting) as required.
# ──────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")/.."

exec .venv/bin/python -m threads_operator.activity_collector_cli "$@"
