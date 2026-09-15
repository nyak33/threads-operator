"""Dedicated compatibility entry point for account-aware Activity collection."""
from __future__ import annotations

import sys

from .operator_cli import main as operator_main


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    return operator_main(["activity-follow", *args])


if __name__ == "__main__":
    raise SystemExit(main())
