#!/usr/bin/env bash
# Activity Follow collector — deterministic, zero-LLM, read-only.
# Ensure the browser is alive+authed (local-only CDP 43399), then run
# one persisting collection pass. Locking prevents overlapping runs.
set -euo pipefail

LOCK=/tmp/threads-activity-collect.lock
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[activity-collect] another run holds the lock, skipping"
  exit 0
fi

OPERATOR_HOME="${THREADS_OPERATOR_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ACCOUNT="${THREADS_ACCOUNT:-syaqir}"
ACCOUNT_ENV="${THREADS_ACCOUNT_ENV:-${HOME}/.threads-operator/accounts/${ACCOUNT}.env}"

# 1. ensure-browser (exits 4 = login required, 5 = failure)
"${OPERATOR_HOME}/.venv/bin/python" \
  "${OPERATOR_HOME}/scripts/threads_activity_ensure_browser.py"

# 2. collect + persist (account-scoped; posting untouched — this path never posts)
cd "$OPERATOR_HOME"
set -a; . "$ACCOUNT_ENV"; set +a
timeout 240 .venv/bin/python scripts/run_activity.py --account "$ACCOUNT" \
  2>&1 | tail -c 400
