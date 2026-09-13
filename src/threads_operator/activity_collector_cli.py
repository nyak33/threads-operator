"""Dedicated module entry point for Activity Follow collection."""
from __future__ import annotations

import sys

from .activity_cli import main as _main


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    return _main(["activity-follow", *args])


if __name__ == "__main__":
    raise SystemExit(main())
