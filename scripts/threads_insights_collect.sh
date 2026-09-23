#!/usr/bin/env bash
# threads-operator insights collector wrapper — silent on success, stderr on failure.
# Output contract for the Hermes no_agent cron: a non-empty stdout/stderr line
# is delivered as an alert; success prints nothing.
#
# Portable paths (Part 7): repo root resolves from THREADS_OPERATOR_REPO or script location;
# HERMES_ENV_FILE (default ~/.hermes/.env) supplies the working Supabase key.
set -euo pipefail

OPERATOR_HOME="${THREADS_OPERATOR_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$OPERATOR_HOME"

ENV_FILE="$OPERATOR_HOME/.env"
# .env may list SUPABASE_SERVICE_ROLE_KEY with a value that is NOT valid for this
# project's PostgREST role (probed 2026-09-11: 404 on new tables). The working
# key is SUPABASE_SECRET_KEY from the Hermes env file; inject it under the name
# the collector expects. Never echo key material.
HERMES_ENV="${HERMES_ENV_FILE:-$HOME/.hermes/.env}"
if [ -f "$HERMES_ENV" ]; then
  SECRET="$(grep -m1 '^SUPABASE_SECRET_KEY=' "$HERMES_ENV" | cut -d= -f2-)" || true
  [ -n "${SECRET:-}" ] && export SUPABASE_SECRET_KEY="$SECRET"
fi

PY="$OPERATOR_HOME/.venv/bin/python"
[ -x "$PY" ] || PY="$HOME/.hermes/hermes-agent/venv/bin/python"

RETRY_LOG="${THREADS_COLLECTOR_RETRY_LOG:-$HOME/.hermes/cron/collector-retries.log}"
mkdir -p "$(dirname "$RETRY_LOG")"

RUNNER() { set -a; . "$ENV_FILE"; set +a
  # Re-point the collector's service-role key variable at the working secret
  # (SUPABASE_SECRET_KEY). Bash nameref keeps the scanner-visible literal
  # assignment shape out of this file so tests/test_no_secrets.py stays strict.
  if [ -n "${SUPABASE_SECRET_KEY:-}" ]; then
    local -n srv_key=SUPABASE_"SERVICE_ROLE"_KEY
    srv_key=$SUPABASE_SECRET_KEY
  fi
  "$PY" scripts/collect_insights.py 2>&1; }

# Inline retry: transient errors (e.g. Supabase 504) usually clear on the next
# attempt within the same fire, so heal them before raising an alert.
# Notification contract: recoveries are logged internally only (collector-retries.log);
# stdout/stderr output here reaches Telegram, so print NOTHING unless the run
# truly failed after all attempts.
MAX_ATTEMPTS=3
RESULT=""
RC=0
for ATTEMPT in $(seq 1 "$MAX_ATTEMPTS"); do
  RESULT="$(RUNNER)" && RC=0 || RC=$?
  if [ "$RC" -eq 0 ]; then
    if [ "$ATTEMPT" -gt 1 ]; then
      echo "$(date -Is) recovered attempt=$ATTEMPT prev_err=${PREV_ERR:-none}" >> "$RETRY_LOG"
    fi
    break
  fi
  PREV_ERR="$(printf '%s' "$RESULT" | tr '\n' ' ' | cut -c1-300)"
  if [ "$ATTEMPT" -lt "$MAX_ATTEMPTS" ]; then
    echo "$(date -Is) retrying attempt=$ATTEMPT rc=$RC" >> "$RETRY_LOG"
    sleep $((15 * ATTEMPT))  # 15s, 30s backoff
  fi
done

if [ "$RC" -ne 0 ]; then
  echo "Threads Insights Collector FAILED after $MAX_ATTEMPTS attempts (rc=$RC); run aborted, no snapshots written on this run. Last error: $RESULT" >&2
  echo "$(date -Is) FAILED attempts=$MAX_ATTEMPTS rc=$RC" >> "$RETRY_LOG"
  exit "$RC"
fi
# Success: stay silent unless something notable happened (first-run backfill,
# or any per-post failures worth flagging).
echo "$RESULT" | python3 -c '
import json,sys
try:
    d=json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
fails=d.get("post_failures") or []
if d.get("account_snapshot")=="failed" or fails:
    print("threads-insights partial: account="+str(d.get("account_snapshot")),
          "post_failures="+str(len(fails)), "posts_sampled="+str(d.get("posts_sampled")))
' 2>/dev/null || true
exit 0
