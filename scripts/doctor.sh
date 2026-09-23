#!/usr/bin/env bash
# threads-operator doctor — one diagnostic command.
#
# Checks Python, deps, config, accounts, Supabase, schema, Telegram, Hermes
# integration, runtime dirs, and runtime jobs. Prints PASS / WARN / FAIL lines
# and a summary. NEVER prints secret values. Exit 1 if any FAIL, else 0.
#
# Usage: scripts/doctor.sh [--account <key>]   (default account: $THREADS_ACCOUNT or "syaqir")
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACCOUNT="${THREADS_ACCOUNT:-syaqir}"
while [ $# -gt 0 ]; do
  case "$1" in
    --account) ACCOUNT="${2:?--account needs a key}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

. "$(dirname "${BASH_SOURCE[0]}")/lib/env.sh"

PASS_COUNT=0; WARN_COUNT=0; FAIL_COUNT=0
pass() { PASS_COUNT=$((PASS_COUNT+1)); echo "PASS  $1"; }
warn() { WARN_COUNT=$((WARN_COUNT+1)); echo "WARN  $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT+1)); echo "FAIL  $1"; }

VENV_PY="$ROOT_DIR/.venv/bin/python"
HERMES_ENV="${HERMES_ENV_FILE:-$HOME/.hermes/.env}"
OPERATOR_HOME="${THREADS_OPERATOR_HOME:-$HOME/.threads-operator}"
ACCOUNT_FILE="$OPERATOR_HOME/accounts/$ACCOUNT.env"

# --- Python / runtime ---------------------------------------------------------
if [ -x "$VENV_PY" ]; then
  PYVER="$("$VENV_PY" -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)"
  if "$VENV_PY" -c 'import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null; then
    pass "Python $PYVER (venv, >=3.11 required)"
  else
    fail "venv Python $PYVER is <3.11"
  fi
else
  fail "venv missing at $ROOT_DIR/.venv — run scripts/bootstrap.sh"
fi

# --- package / CLI ------------------------------------------------------------
if [ -x "$ROOT_DIR/.venv/bin/threads-operator" ]; then
  if "$VENV_PY" -c 'import threads_operator' 2>/dev/null; then
    pass "threads_operator package importable, CLI present"
  else
    fail "threads_operator package not importable in venv"
  fi
else
  fail "threads-operator CLI missing — run scripts/bootstrap.sh"
fi

# --- account config -----------------------------------------------------------
if [ -f "$ACCOUNT_FILE" ]; then
  pass "account file exists ($ACCOUNT)"
  MISSING=""
  for KEY in THREADS_ACCESS_TOKEN THREADS_USER_ID SUPABASE_URL SUPABASE_SERVICE_ROLE_KEY; do
    VAL="$(grep -m1 "^$KEY=" "$ACCOUNT_FILE" 2>/dev/null | cut -d= -f2- | tr -d "\"'")"
    if [ -z "$VAL" ] || [ "$VAL" = "replace_me" ]; then MISSING="$MISSING $KEY"; fi
  done
  if [ -n "$MISSING" ]; then
    fail "account '$ACCOUNT' missing/placeholder required values:$MISSING"
  else
    pass "account '$ACCOUNT' has all required credential values (not shown)"
  fi
else
  fail "no account file at $OPERATOR_HOME/accounts/$ACCOUNT.env — run scripts/add_account.sh $ACCOUNT"
fi

# --- Supabase connectivity + schema -------------------------------------------
if [ -x "$VENV_PY" ] && [ -f "$ACCOUNT_FILE" ]; then
  SCHEMA_OUT="$(THREADS_ACCOUNT="$ACCOUNT" "$VENV_PY" - <<'PY' 2>&1
import json, os, sys, urllib.request
sys.path.insert(0, os.path.join(os.getcwd(), "src"))
from threads_operator.account_config import load_account_config  # noqa
try:
    cfg = load_account_config(os.environ.get("THREADS_ACCOUNT"))
except Exception as exc:  # noqa: BLE001
    print(json.dumps({"error": f"config: {exc}"})); raise SystemExit(0)
url = cfg.require("SUPABASE_URL").rstrip("/")
key = cfg.require("SUPABASE_SERVICE_ROLE_KEY")
tables = [
    "threads_publish_queue", "threads_trend_candidates", "threads_posts",
    "threads_inbound_replies", "threads_engagement_queue", "threads_engagement_config",
    "threads_account_insights_snapshots", "threads_post_insights_snapshots",
    "threads_daily_rollups", "threads_activity_events",
    "threads_post_daily_rollups", "threads_gateway_keys",
]
missing, errors = [], []
for t in tables:
    req = urllib.request.Request(
        f"{url}/rest/v1/{t}?select=*&limit=0",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except urllib.error.HTTPError as exc:
        (missing if exc.code == 404 else errors).append(f"{t}:{exc.code}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{t}:{type(exc).__name__}")
print(json.dumps({"missing": missing, "errors": errors}))
PY
)"
  if echo "$SCHEMA_OUT" | grep -q '"error"'; then
    warn "Supabase probe could not run: $(echo "$SCHEMA_OUT" | head -c 200)"
  else
    MISSING_T="$(echo "$SCHEMA_OUT" | "$VENV_PY" -c 'import json,sys;print(" ".join(json.load(sys.stdin)["missing"]))' 2>/dev/null)"
    ERRORS_T="$(echo "$SCHEMA_OUT" | "$VENV_PY" -c 'import json,sys;print(" ".join(json.load(sys.stdin)["errors"]))' 2>/dev/null)"
    if [ -z "$ERRORS_T" ]; then
      pass "Supabase reachable (PostgREST)"
    else
      fail "Supabase probe errors: $ERRORS_T"
    fi
    # only claim schema completeness when every table probed cleanly
    if [ -z "$ERRORS_T" ] && [ -z "$MISSING_T" ]; then
      pass "all 12 expected tables present"
    elif [ -n "$MISSING_T" ]; then
      if echo "$MISSING_T" | grep -q "threads_post_daily_rollups"; then
        warn "missing tables: $MISSING_T (threads_post_daily_rollups needs migrations/002_post_daily_rollups.sql — known pending step)"
      else
        fail "missing tables: $MISSING_T — apply migrations (013 reconciles production-only tables)"
      fi
    fi
  fi
fi

# --- Telegram ------------------------------------------------------------------
TG_TOKEN="$( [ -f "$HERMES_ENV" ] && grep -m1 '^TELEGRAM_BOT_TOKEN=' "$HERMES_ENV" | cut -d= -f2- | tr -d "\"'" )"
TG_CHAT="$(  [ -f "$HERMES_ENV" ] && grep -m1 '^TELEGRAM_HOME_CHANNEL=' "$HERMES_ENV" | cut -d= -f2- | tr -d "\"'" )"
[ -n "${TG_CHAT:-}" ] || TG_CHAT="$( [ -f "$HERMES_ENV" ] && grep -m1 '^TELEGRAM_CHAT_ID=' "$HERMES_ENV" | cut -d= -f2- | tr -d "\"'" )"
[ -n "${TG_CHAT:-}" ] || TG_CHAT="${TELEGRAM_CHAT_ID:-}"
if [ -n "${TG_TOKEN:-}" ] && [ -n "${TG_CHAT:-}" ]; then
  pass "Telegram bot token + chat id present (values not shown)"
else
  [ -n "${TG_TOKEN:-}" ] || warn "TELEGRAM_BOT_TOKEN not found in $HERMES_ENV — approval cards will not send"
  [ -n "${TG_CHAT:-}" ]  || warn "no Telegram chat id (TELEGRAM_HOME_CHANNEL/TELEGRAM_CHAT_ID) — alerts no-op"
fi

# --- Hermes integration --------------------------------------------------------
if [ -f "$HERMES_ENV" ]; then
  pass "Hermes env file present ($HERMES_ENV)"
else
  warn "Hermes env file not found at $HERMES_ENV (set HERMES_ENV_FILE if elsewhere)"
fi
JOBS_JSON="$HOME/.hermes/cron/jobs.json"
if [ -f "$JOBS_JSON" ]; then
  pass "Hermes cron jobs.json present"
  if [ -x "$VENV_PY" ]; then
    "$VENV_PY" - "$JOBS_JSON" "$ROOT_DIR" <<'PY'
import json, sys
jobs = json.load(open(sys.argv[1]))["jobs"]
repo = sys.argv[2]
ours = [j for j in jobs if j.get("script") and (
    "threads" in j["script"] or "trend_engagement" in j["script"] or "own_replies" in j["script"])]
bad = [j["name"] for j in ours if not j.get("enabled", True)]
missing = [j["name"] for j in ours if not (j.get("script"))]
print(f"INFO  {len(ours)} threads-operator Hermes cron job(s) installed")
for n in bad:
    print(f"WARN  job paused/disabled: {n}")
PY
  fi
else
  warn "Hermes cron jobs.json not found — runtime jobs not installed (run scripts/install_jobs.sh)"
fi

# --- runtime directories -------------------------------------------------------
for DIR in "$OPERATOR_HOME" "$OPERATOR_HOME/accounts" "$OPERATOR_HOME/browser-profiles"; do
  if [ -d "$DIR" ]; then
    PERMS="$(stat -c '%a' "$DIR" 2>/dev/null || echo '?')"
    if [ "$PERMS" = "700" ]; then pass "runtime dir $DIR (700)"; else warn "runtime dir $DIR perms=$PERMS (expected 700)"; fi
  else
    warn "runtime dir missing: $DIR (created by bootstrap.sh)"
  fi
done

# --- summary -------------------------------------------------------------------
echo "---"
echo "doctor: $PASS_COUNT passed, $WARN_COUNT warnings, $FAIL_COUNT failed"
[ "$FAIL_COUNT" -eq 0 ]
