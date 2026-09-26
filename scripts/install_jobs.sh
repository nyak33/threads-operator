#!/usr/bin/env bash
# Install/verify/remove Threads Operator runtime jobs in Hermes cron.
#
# Declarative + idempotent: the desired state lives in config/runtime-jobs.json.
# Re-running converges existing jobs (matched by exact name) to the declared
# schedule/deliver/script instead of duplicating them.
#
# Usage:
#   scripts/install_jobs.sh [--account syaqir] [--dry-run]   # install/verify (default)
#   scripts/install_jobs.sh --status                          # report only, no changes
#   scripts/install_jobs.sh --remove [--account syaqir]       # delete operator jobs
#
# Requires the `hermes` CLI on PATH. NEVER prints secrets.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$ROOT_DIR/config/runtime-jobs.json"
JOBS_JSON="${HERMES_CRON_JOBS_FILE:-$HOME/.hermes/cron/jobs.json}"
ACCOUNT="${THREADS_ACCOUNT:-syaqir}"
MODE="install"
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --account) ACCOUNT="${2:?--account needs a key}"; shift 2 ;;
    --status)  MODE="status"; shift ;;
    --remove)  MODE="remove"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ "$MODE" != "status" ] && ! command -v hermes >/dev/null 2>&1; then
  echo "FAIL  hermes CLI not on PATH — runtime jobs need Hermes installed" >&2
  exit 1
fi

PY="${ROOT_DIR}/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

# Sync operator watchdog scripts into HERMES_HOME/scripts/. The Hermes cron scheduler
# refuses to run any script that resolves outside HERMES_HOME/scripts/ (traversal/symlink
# guard), so the repo copies there must be refreshed on every install to pick up repo
# changes. No secrets are involved — these are plain Python sources already in git.
if [ "$MODE" != "remove" ]; then
  HERMES_SCRIPTS_DIR="${HERMES_SCRIPTS_DIR:-$HOME/.hermes/scripts}"
  mkdir -p "$HERMES_SCRIPTS_DIR"
  for src in "$ROOT_DIR"/scripts/*watchdog*.py; do
    [ -f "$src" ] || continue
    base="$(basename "$src")"
    dest="$HERMES_SCRIPTS_DIR/$base"
    if [ "$DRY_RUN" -eq 1 ]; then
      cmp -s "$src" "$dest" 2>/dev/null || echo "WOULD-SYNC $base -> $HERMES_SCRIPTS_DIR/"
    elif ! cmp -s "$src" "$dest" 2>/dev/null; then
      cp "$src" "$dest" && chmod 700 "$dest" && echo "SYNCED   $base -> $HERMES_SCRIPTS_DIR/"
    fi
  done
fi

# One Python pass renders the manifest, matches existing jobs from the durable
# store, and prints an action plan (shell-safe lines) for bash to execute.
PLAN="$(HERMES_JOBS_JSON="$JOBS_JSON" "$PY" - "$MANIFEST" "$ACCOUNT" "$ROOT_DIR" "$MODE" <<'PY'
import json, os, shlex, sys

manifest_path, account, repo, mode = sys.argv[1:5]
manifest = json.load(open(manifest_path))

store_path = os.environ.get("HERMES_JOBS_JSON", "")
existing = {}
try:
    raw = json.load(open(store_path))
    for j in raw.get("jobs", []):
        existing[j.get("name", "")] = j
except Exception:
    pass

def q(s):
    return shlex.quote(str(s))

for j in manifest["jobs"]:
    name = j["name"].replace("{{ACCOUNT}}", account)
    sched = j["schedule"]
    sched_arg = f"{sched['minutes']}m" if sched["kind"] == "interval" else sched["expr"]
    deliver = j.get("deliver", "local")
    live = existing.get(name)

    if mode == "status":
        state = f"OK id={live['id']} enabled={live.get('enabled')}" if live else "MISSING"
        print(f"STATUS\t{q(name)}\t{q(state)}")
        continue
    if mode == "remove":
        if live:
            print(f"REMOVE\t{q(name)}\t{q(live['id'])}")
        else:
            print(f"ABSENT\t{q(name)}")
        continue
    # install
    if j["kind"] == "script":
        script_abs = os.path.join(repo, j["script"])
        if not os.path.isfile(script_abs):
            print(f"FAIL\t{q(name)}\tmissing script {q(j['script'])}")
            continue
        create_args = f"--script {q(script_abs)} --no-agent --deliver {q(deliver)} --failure-deliver origin"
    else:
        tmpl = open(os.path.join(repo, j["prompt_template"])).read()
        prompt = tmpl.replace("{{REPO}}", repo).replace("{{ACCOUNT}}", account)
        create_args = f"--deliver {q(deliver)} --failure-deliver origin --workdir {q(repo)}"
    if live:
        print(f"EDIT\t{q(name)}\t{q(live['id'])}\t{q(sched_arg)}\t{q(deliver)}")
    elif j["kind"] == "prompt":
        import base64
        print(f"CREATEP\t{q(name)}\t{q(sched_arg)}\t{create_args}\t{base64.b64encode(prompt.encode()).decode()}")
    else:
        print(f"CREATE\t{q(name)}\t{q(sched_arg)}\t{create_args}")
PY
)"

ADDED=0; UPDATED=0; REMOVED=0; OK=0; MISSING=0; FAILED=0

while IFS=$'\t' read -r ACTION F1 F2 F3 F4; do
  [ -n "${ACTION:-}" ] || continue
  # planner emits shlex-quoted fields; eval strips the quoting exactly once
  eval "F1=$F1"; eval "F2=$F2"; eval "F3=${F3:-''}"; eval "F4=${F4:-''}"
  case "$ACTION" in
    STATUS)
      echo "  $F1 -> $F2"
      case "$F2" in MISSING*) MISSING=$((MISSING+1)) ;; *) OK=$((OK+1)) ;; esac ;;
    ABSENT)
      echo "ABSENT   $F1" ;;
    REMOVE)
      if [ "$DRY_RUN" -eq 1 ]; then echo "WOULD-RM $F1 (id=$F2)"; REMOVED=$((REMOVED+1));
      elif hermes cron remove "$F2" >/dev/null 2>&1; then echo "REMOVED  $F1"; REMOVED=$((REMOVED+1));
      else echo "FAIL-RM  $F1"; FAILED=$((FAILED+1)); fi ;;
    EDIT)
      if [ "$DRY_RUN" -eq 1 ]; then echo "WOULD-UPD $F1 (id=$F2 schedule='$F3' deliver=$F4)"; UPDATED=$((UPDATED+1));
      elif hermes cron edit "$F2" --schedule "$F3" --deliver "$F4" >/dev/null 2>&1; then
        echo "OK       $F1 (id=$F2, converged)"; UPDATED=$((UPDATED+1))
      else echo "FAIL-UPD $F1"; FAILED=$((FAILED+1)); fi ;;
    CREATE)
      if [ "$DRY_RUN" -eq 1 ]; then echo "WOULD-ADD $F1 (schedule='$F2' $F3)"; ADDED=$((ADDED+1));
      else
        # F3 holds pre-quoted CLI args; eval is safe here — args were shlex.quote'd by the planner
        if eval "hermes cron create \"\$F2\" --name \"\$F1\" $F3" >/dev/null 2>&1; then
          echo "ADDED    $F1"; ADDED=$((ADDED+1))
        else echo "FAIL-ADD $F1"; FAILED=$((FAILED+1)); fi
      fi ;;
    CREATEP)
      if [ "$DRY_RUN" -eq 1 ]; then echo "WOULD-ADD $F1 (agent prompt, schedule='$F2' $F3)"; ADDED=$((ADDED+1));
      else
        PROMPT_TEXT="$(printf '%s' "$F4" | base64 -d)"
        if PROMPT_TEXT="$PROMPT_TEXT" bash -c 'eval "hermes cron create \"$0\" --name \"$1\" $2 \"$PROMPT_TEXT\""' "$F2" "$F1" "$F3" >/dev/null 2>&1; then
          echo "ADDED    $F1 (agent prompt)"; ADDED=$((ADDED+1))
        else echo "FAIL-ADD $F1"; FAILED=$((FAILED+1)); fi
      fi ;;
    FAIL)
      echo "FAIL     $F1 — $F2"; FAILED=$((FAILED+1)) ;;
  esac
done <<< "$PLAN"

echo "---"
case "$MODE" in
  status)  echo "status: $OK present, $MISSING missing (account=$ACCOUNT)"; [ "$MISSING" -eq 0 ] ;;
  remove)  echo "remove: $REMOVED removed, $FAILED failed$([ $DRY_RUN -eq 1 ] && echo ' (dry-run)')"; [ "$FAILED" -eq 0 ] ;;
  install) echo "install: $ADDED added, $UPDATED converged, $FAILED failed$([ $DRY_RUN -eq 1 ] && echo ' (dry-run)')"; [ "$FAILED" -eq 0 ] ;;
esac
