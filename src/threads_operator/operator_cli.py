"""Unified account-aware CLI for Threads Operator deployments."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from collections.abc import Mapping
from typing import Any

from .account_config import (
    AccountConfig,
    AccountConfigError,
    list_accounts,
    load_account_config,
    resolve_account_name,
)
from .activity_collector import collect_activity_follows
from .collector import collect_once
from .publisher import publish_next
from .supabase_store import SupabaseStore
from .threads_api import DEFAULT_BASE_URL, ThreadsAPI

_EXECUTION_MODES = {"draft_only", "approval_required", "auto_post"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="threads-operator",
        description="Portable multi-account Threads operator",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    accounts = sub.add_parser("accounts", help="Inspect configured account keys")
    accounts_sub = accounts.add_subparsers(dest="accounts_command", required=True)
    accounts_sub.add_parser("list", help="List local account configs")

    doctor = sub.add_parser("doctor", help="Validate one account without posting")
    doctor.add_argument("--account")

    insights = sub.add_parser("insights", help="Collect official Threads Insights")
    insights.add_argument("--account")

    activity = sub.add_parser(
        "activity-follow", help="Read the authenticated Threads Activity/Follows feed"
    )
    activity.add_argument("--account")
    activity.add_argument("--dry-run", action="store_true")
    activity.add_argument("--settle", type=float, default=3.0)

    publish = sub.add_parser("publish", help="Publish one approved due queue item")
    publish.add_argument("--account")
    publish.add_argument("--dry-run", action="store_true")

    return parser


def _load_selected(
    explicit: str | None,
    process_env: Mapping[str, str],
) -> AccountConfig:
    name = resolve_account_name(explicit, process_env)
    return load_account_config(name, process_env)


def _api(config: AccountConfig) -> ThreadsAPI:
    return ThreadsAPI(
        config.require("THREADS_ACCESS_TOKEN"),
        config.require("THREADS_USER_ID"),
        base_url=config.get("THREADS_API_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL,
    )


def _store(config: AccountConfig) -> SupabaseStore:
    return SupabaseStore(
        config.require("SUPABASE_URL"),
        config.require("SUPABASE_SERVICE_ROLE_KEY"),
        account_key=config.name,
    )


def _doctor(config: AccountConfig) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "account_file": True,
        "api_credentials": True,
        "supabase_credentials": True,
        "sample_minutes": config.get_int("THREADS_ACCOUNT_SAMPLE_MINUTES"),
    }
    mode = config.get("THREADS_EXECUTION_MODE", "approval_required") or "approval_required"
    if mode not in _EXECUTION_MODES:
        raise AccountConfigError(
            "THREADS_EXECUTION_MODE must be draft_only, approval_required, or auto_post"
        )
    checks["execution_mode"] = mode
    checks["posting_enabled"] = config.get_bool("THREADS_POSTING_ENABLED")
    checks["activity_enabled"] = config.get_bool("ACTIVITY_FOLLOW_COLLECTOR_ENABLED")

    browser_profile = config.get("THREADS_BROWSER_PROFILE", "") or ""
    if checks["activity_enabled"]:
        if not browser_profile:
            raise AccountConfigError(
                "THREADS_BROWSER_PROFILE is required when Activity collection is enabled"
            )
        profile_path = Path(browser_profile).expanduser()
        if not profile_path.is_dir():
            raise AccountConfigError(
                "THREADS_BROWSER_PROFILE directory does not exist for enabled Activity collection"
            )
        checks["browser_profile"] = True
    else:
        checks["browser_profile"] = bool(browser_profile)

    warnings: list[str] = []
    if checks["posting_enabled"] and mode != "auto_post":
        warnings.append(
            "THREADS_POSTING_ENABLED is true but execution mode is not auto_post; live posting remains blocked"
        )
    return {
        "ok": True,
        "account": config.name,
        "checks": checks,
        "warnings": warnings,
    }


def _run_insights(config: AccountConfig) -> tuple[int, dict[str, Any]]:
    summary = collect_once(
        _api(config),
        _store(config),
        account_sample_minutes=config.get_int("THREADS_ACCOUNT_SAMPLE_MINUTES"),
    )
    return 0, {"ok": True, "account": config.name, **summary}


def _run_activity(
    config: AccountConfig,
    *,
    dry_run: bool,
    settle: float,
) -> tuple[int, dict[str, Any]]:
    enabled = config.get_bool("ACTIVITY_FOLLOW_COLLECTOR_ENABLED")
    if not dry_run and not enabled:
        return 3, {
            "ok": False,
            "account": config.name,
            "error": "Activity Follow Collector is disabled for this account",
        }
    profile_raw = config.get("THREADS_BROWSER_PROFILE", "") or ""
    if not profile_raw:
        raise AccountConfigError("THREADS_BROWSER_PROFILE is required for Activity collection")
    owned_posts = _api(config).list_posts()
    result = collect_activity_follows(
        store=_store(config),
        profile_dir=Path(profile_raw).expanduser(),
        own_posts=owned_posts,
        settle_seconds=settle,
        persist=not dry_run,
    )
    success = bool(result.get("success"))
    return (0 if success else 1), {
        "ok": success,
        "account": config.name,
        **result,
    }


def _run_publish(
    config: AccountConfig,
    *,
    dry_run: bool,
) -> tuple[int, dict[str, Any]]:
    enabled = config.get_bool("THREADS_POSTING_ENABLED")
    mode = config.get("THREADS_EXECUTION_MODE", "approval_required") or "approval_required"
    if not dry_run and not (enabled and mode == "auto_post"):
        return 3, {
            "status": "disabled",
            "account": config.name,
            "error": (
                "Live posting is disabled; require THREADS_POSTING_ENABLED=true "
                "and THREADS_EXECUTION_MODE=auto_post"
            ),
        }
    table = config.get("THREADS_QUEUE_TABLE", "threads_publish_queue") or "threads_publish_queue"
    campaign = config.get("THREADS_QUEUE_CAMPAIGN_CODE", "") or None
    result = publish_next(
        _api(config),
        _store(config),
        table,
        campaign_code=campaign,
        dry_run=dry_run,
    )
    result = {"account": config.name, **result}
    return (1 if result.get("status") == "failed" else 0), result


def main(
    argv: list[str] | None = None,
    *,
    process_env: Mapping[str, str] | None = None,
) -> int:
    env = process_env if process_env is not None else os.environ
    args = _parser().parse_args(argv)

    if args.command == "accounts" and args.accounts_command == "list":
        for account in list_accounts(env):
            print(account)
        return 0

    try:
        config = _load_selected(getattr(args, "account", None), env)
        if args.command == "doctor":
            code, payload = 0, _doctor(config)
        elif args.command == "insights":
            code, payload = _run_insights(config)
        elif args.command == "activity-follow":
            code, payload = _run_activity(
                config, dry_run=args.dry_run, settle=args.settle
            )
        elif args.command == "publish":
            code, payload = _run_publish(config, dry_run=args.dry_run)
        else:
            raise RuntimeError(f"Unsupported command: {args.command}")
    except AccountConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1

    serialized = json.dumps(payload, separators=(",", ":"))
    if code == 3:
        print(payload.get("error", serialized), file=sys.stderr)
    else:
        print(serialized)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
