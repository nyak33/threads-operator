"""Publish-queue worker — Hermes cron wrapper (*/N min).

Deterministic watchdog for the account's publish queue. Runs the
threads-operator ``publish-worker`` command, which publishes the oldest due
approved row and automatically requeues transient failures to ``approved``
for the next tick. Silence when idle; non-zero exit fires the cron failure
alert.

Deploy as a Hermes cron job (see docs/publish-queue-worker.md), e.g. every
5 minutes. Adjust ACCOUNT and paths for your installation.
"""

import subprocess
import sys
from pathlib import Path

# The account whose queue this worker publishes. One cron job per account.
ACCOUNT = "syaqir"

# Optional: restrict the worker to a single campaign. Leave as None to publish
# any due approved row for the account (recommended for a generic pipeline).
CAMPAIGN_CODE = None

OPERATOR_CLI = Path.home() / "threads-operator" / ".venv" / "bin" / "threads-operator"


def main() -> None:
    args = [str(OPERATOR_CLI), "publish-worker", "--account", ACCOUNT]
    if CAMPAIGN_CODE:
        args += ["--campaign-code", CAMPAIGN_CODE]
    args += sys.argv[1:]  # pass through e.g. --dry-run

    result = subprocess.run(args, capture_output=True, text=True, timeout=600)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
