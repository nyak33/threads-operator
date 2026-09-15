"""Legacy command-line entrypoints for Insights and Activity collection.

New multi-account deployments should use ``threads_operator.operator_cli``.
This module remains for backward compatibility with existing scripts.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import sys

from .supabase_store import SupabaseStore
from .activity_collector import collect_activity_follows
from .activity_browser import ACTIVITY_URL

DEFAULT_THREADS_API_BASE_URL = "https://graph.threads.net/v1.0"


def _load_env(base_dir: str | None = None) -> dict[str, str]:
    env: dict[str, str] = {**os.environ}
    dirs = [pathlib.Path(base_dir or "").resolve(), pathlib.Path.cwd()]
    seen: set[pathlib.Path] = set()
    for d in reversed(dirs):
        if d in seen:
            continue
        seen.add(d)
        f = d / ".env"
        if f.is_file():
            for line in f.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _setup_logging(level: str = "WARNING") -> None:
    fmt = "%(asctime)s %(levelname)-8s %(name)s %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.WARNING), format=fmt
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="threads-operator-legacy",
        description="Legacy Threads Insights & Activity Follow collector",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    ip = sub.add_parser("insights", help="Run official Threads Insights collection")
    ip.add_argument("--post-file", type=pathlib.Path)
    ip.add_argument("--profile-dir", default=None)
    ap = sub.add_parser(
        "activity-follow", help="Read /activity/follows and store results"
    )
    ap.add_argument("--dry-run", action="store_true", help="Parse only; do not write to Supabase")
    ap.add_argument("--settle", type=float, default=3.0)
    return parser


def cmd_insights(env: dict[str, str], args: argparse.Namespace) -> int:
    from .collector import collect_once

    api_cls = __import__("threads_operator.threads_api", fromlist=["ThreadsAPI"]).ThreadsAPI
    api = api_cls(
        env["THREADS_ACCESS_TOKEN"],
        env.get("THREADS_USER_ID", ""),
        base_url=env.get("THREADS_API_BASE_URL") or DEFAULT_THREADS_API_BASE_URL,
    )
    store = SupabaseStore(
        base_url=env["SUPABASE_URL"],
        service_role_key=env["SUPABASE_SERVICE_ROLE_KEY"],
        account_key=env.get("THREADS_ACCOUNT") or env.get("THREADS_USER_ID"),
    )
    try:
        summary = collect_once(
            api,
            store,
            account_sample_minutes=int(env.get("THREADS_ACCOUNT_SAMPLE_MINUTES", "15")),
        )
    except Exception as exc:
        print(
            json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"ok": True, **summary}, separators=(",", ":")))
    return 0


def cmd_activity_follow(
    env: dict[str, str], args: argparse.Namespace, profile_dir: pathlib.Path
) -> int:
    account_key = env.get("THREADS_ACCOUNT") or env.get("THREADS_USER_ID")
    if not args.dry_run and not account_key:
        print(
            "Legacy Activity persistence requires THREADS_ACCOUNT or THREADS_USER_ID.",
            file=sys.stderr,
        )
        return 2
    store = SupabaseStore(
        base_url=env["SUPABASE_URL"],
        service_role_key=env["SUPABASE_SERVICE_ROLE_KEY"],
        account_key=account_key,
    )
    print(f"[activity-follow] reading {ACTIVITY_URL} …")
    result = collect_activity_follows(
        store=store,
        profile_dir=profile_dir,
        own_posts=None,
        settle_seconds=args.settle,
        persist=not args.dry_run,
    )
    ok = result.pop("success", False)
    total = result.get("total_events", 0)
    new_events = result.get("new_events", 0)
    high = result.get("high", 0)
    medium = result.get("medium", 0)
    low = result.get("low", 0)
    unknown = result.get("unknown", 0)
    print(
        json.dumps(
            {
                "status": "ok" if ok else "failed",
                "challenge_seen": result.pop("challenge_seen", False),
                "total_events": total,
                "new_events_stored": new_events,
                "high_confidence": high,
                "medium_confidence": medium,
                "low_confidence": low,
                "unresolved": unknown,
            },
            indent=2,
        )
    )
    if args.dry_run and ok:
        print("[activity-follow] dry-run complete — nothing written to DB")
        return 0
    if ok:
        print(
            f"[activity-follow] done — {total} events, {new_events} new, "
            f"{high}H/{medium}M/{low}L/{unknown}U"
        )
        return 0
    print(f"[activity-follow] failed: {result.get('error', 'unknown')}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    env = _load_env()
    _setup_logging(env.get("LOG_LEVEL", "WARNING"))
    profile_dir = pathlib.Path(
        env.get("THREADS_BROWSER_PROFILE", "~/.hermes/browser-profiles/threads")
    ).expanduser()
    try:
        if args.command == "insights":
            return cmd_insights(env, args)
        if args.command == "activity-follow":
            enabled = env.get("ACTIVITY_FOLLOW_COLLECTOR_ENABLED", "false").lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            if not args.dry_run and not enabled:
                print(
                    "Activity Follow Collector is disabled; set "
                    "ACTIVITY_FOLLOW_COLLECTOR_ENABLED=true to persist.",
                    file=sys.stderr,
                )
                return 3
            return cmd_activity_follow(env, args, profile_dir)
        parser.print_help()
        return 1
    except Exception:
        logging.getLogger(__name__).exception("unhandled exception")
        return 2


if __name__ == "__main__":
    sys.exit(main())
