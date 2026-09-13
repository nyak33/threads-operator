#!/usr/bin/env python3
"""Compatibility runner for the Activity Follow collector."""
from __future__ import annotations

import sys
from threads_operator.activity_collector_cli import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
