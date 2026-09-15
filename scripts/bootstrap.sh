#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
OPERATOR_HOME="${THREADS_OPERATOR_HOME:-$HOME/.threads-operator}"

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("threads-operator requires Python 3.11+")
print(f"Python {sys.version_info.major}.{sys.version_info.minor} OK")
PY

if [[ ! -d "$ROOT_DIR/.venv" ]]; then
    "$PYTHON_BIN" -m venv "$ROOT_DIR/.venv"
fi

"$ROOT_DIR/.venv/bin/python" -m pip install --upgrade pip
"$ROOT_DIR/.venv/bin/python" -m pip install -e "$ROOT_DIR"

mkdir -p "$OPERATOR_HOME/accounts" "$OPERATOR_HOME/browser-profiles"
chmod 700 "$OPERATOR_HOME" "$OPERATOR_HOME/accounts" "$OPERATOR_HOME/browser-profiles"

echo "threads-operator installed."
echo "THREADS_OPERATOR_HOME=$OPERATOR_HOME"
echo "Next: $ROOT_DIR/scripts/add_account.sh <account-key>"
