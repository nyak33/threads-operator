#!/usr/bin/env bash
# Shared helpers for threads-operator runtime scripts. Source, do not execute:
#   . "$(dirname "${BASH_SOURCE[0]}")/lib/env.sh"
#
# Provides:
#   threads_operator_repo       - repo root (THREADS_OPERATOR_REPO or caller location)
#   threads_operator_load_env   - load KEY=VALUE pairs from a file without executing it
#
# The watchdogs historically sourced the Hermes env file with `. "$FILE"`, which
# EXECUTES any shell in it. These helpers parse instead of source.

# Resolve repo root: explicit env override wins, else relative to this file.
threads_operator_repo() {
  if [ -n "${THREADS_OPERATOR_REPO:-}" ]; then
    printf '%s\n' "$THREADS_OPERATOR_REPO"
  else
    (cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
  fi
}

# Export KEY=VALUE lines from an env file WITHOUT executing it.
# Skips blanks/comments, strips optional `export ` prefix and surrounding quotes.
# Usage: threads_operator_load_env /path/to/.env KEY1 KEY2 ...
# With no KEY args, exports every valid KEY=VALUE pair found.
threads_operator_load_env() {
  local file="$1"; shift || true
  [ -f "$file" ] || return 0
  local line key val
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#export }"
    case "$line" in ''|\#*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"
    val="${line#*=}"
    # strip surrounding quotes
    case "$val" in
      \"*\") val="${val#\"}"; val="${val%\"}" ;;
      \'*\') val="${val#\'}"; val="${val%\'}" ;;
    esac
    if [ "$#" -gt 0 ]; then
      local want=0 k
      for k in "$@"; do [ "$k" = "$key" ] && want=1 && break; done
      [ "$want" -eq 1 ] || continue
    fi
    export "$key=$val"
  done < "$file"
}
