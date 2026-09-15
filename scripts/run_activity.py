#!/usr/bin/env python3
"""Compatibility runner for account-aware Activity Follow collection."""
from __future__ import annotations

import sys

from threads_operator.operator_cli import main


if __name__ == "__main__":
    raise SystemExit(main(["activity-follow", *sys.argv[1:]]))
