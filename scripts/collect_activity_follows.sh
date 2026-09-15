#!/usr/bin/env bash
# Compatibility wrapper. Multi-account deployments must pass --account <key>
# (or set THREADS_ACCOUNT) so the correct env/browser/Supabase config is used.
set -euo pipefail

cd "$(dirname "$0")/.."

exec .venv/bin/threads-operator activity-follow "$@"
