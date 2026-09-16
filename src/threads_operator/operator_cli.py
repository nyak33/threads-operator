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
from .safe_errors import redact_error
from .supabase_store import SupabaseStore, TREND_STATUSES, trend_candidate_payload
from .threads_api import DEFAULT_BASE_URL, ThreadsAPI
from .trend_urls import normalize_threads_post_url

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

    enqueue = sub.add_parser(
        "enqueue-draft",
        help="Insert already-generated content into the selected account queue as draft",
    )
    enqueue.add_argument("--account")
    enqueue.add_argument("--text", required=True)
    enqueue.add_argument("--reply", action="append", default=[])
    enqueue.add_argument("--campaign-code")
    enqueue.add_argument("--scheduled-at")

    publish = sub.add_parser("publish", help="Publish one approved due queue item")
    publish.add_argument("--account")
    publish.add_argument("--dry-run", action="store_true")

    trend = sub.add_parser(
        "trend", help="Manage trend content-discovery candidates"
    )
    trend_sub = trend.add_subparsers(dest="trend_command", required=True)
    trend_add = trend_sub.add_parser(
        "add",
        help="Manually add a public Threads post URL as a trend candidate",
    )
    trend_add.add_argument("--account")
    trend_add.add_argument("--url", required=True)
    trend_add.add_argument("--text")
    trend_add.add_argument("--username")
    trend_add.add_argument("--dry-run", action="store_true")
    trend_list = trend_sub.add_parser(
        "list", help="List this account's trend candidates (read-only)"
    )
    trend_list.add_argument("--account")
    trend_list.add_argument("--limit", type=int, default=20)
    trend_list.add_argument("--status", choices=sorted(TREND_STATUSES))
    trend_show = trend_sub.add_parser(
        "show", help="Show one trend candidate (read-only)"
    )
    trend_show.add_argument("--account")
    trend_show.add_argument("--id", type=int, required=True)
    trend_enrich_p = trend_sub.add_parser(
        "enrich",
        help=(
            "Read-only browser enrichment: structured preloader evidence -> "
            "factual source_post_id update (allowlisted, account-scoped)"
        ),
    )
    trend_enrich_p.add_argument("--account")
    trend_enrich_p.add_argument("--id", type=int, help="candidate id (live mode)")
    trend_enrich_p.add_argument("--url", help="permalink (required with --dry-run)")
    trend_enrich_p.add_argument("--dry-run", action="store_true")
    trend_enrich_p.add_argument("--settle", type=float, default=3.0)

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


def _known_secrets(config: AccountConfig | None) -> list[str]:
    if config is None:
        return []
    return [
        config.get("THREADS_ACCESS_TOKEN", "") or "",
        config.get("SUPABASE_SERVICE_ROLE_KEY", "") or "",
    ]


def _sanitize_payload(
    payload: dict[str, Any], config: AccountConfig | None
) -> dict[str, Any]:
    safe = dict(payload)
    if isinstance(safe.get("error"), str):
        safe["error"] = redact_error(safe["error"], _known_secrets(config))
    return safe


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


def _run_enqueue_draft(
    config: AccountConfig,
    *,
    text: str,
    replies: list[str],
    campaign_code: str | None,
    scheduled_at: str | None,
) -> tuple[int, dict[str, Any]]:
    table = config.get("THREADS_QUEUE_TABLE", "threads_publish_queue") or "threads_publish_queue"
    campaign = campaign_code or config.get("THREADS_QUEUE_CAMPAIGN_CODE", "") or None
    row = _store(config).enqueue_draft(
        table,
        text,
        reply_texts=replies,
        campaign_code=campaign,
        scheduled_at=scheduled_at,
    )
    return 0, {
        "ok": True,
        "account": config.name,
        "id": row.get("id"),
        "status": row.get("status", "draft"),
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


def _run_trend_add(
    config: AccountConfig,
    *,
    url: str,
    text: str | None,
    username: str | None,
    dry_run: bool,
) -> tuple[int, dict[str, Any]]:
    # The selected --account is authoritative: target_account_id always comes
    # from config.name; there is no flag to override it.
    candidate = trend_candidate_payload(
        config.name,
        url,
        source_username=username,
        source_text=text,
    )
    if dry_run:
        return 0, {
            "ok": True,
            "account": config.name,
            "dry_run": True,
            "writes": 0,
            "candidate": candidate,
        }
    result = _store(config).insert_trend_candidate(
        url, source_username=username, source_text=text
    )
    return 0, {
        "ok": True,
        "account": config.name,
        "dry_run": False,
        **result,
    }


def _run_trend_list(
    config: AccountConfig, *, limit: int, status: str | None
) -> tuple[int, dict[str, Any]]:
    # Read-only: SELECT via store list_trend_candidates; account isolation is
    # enforced inside the store layer against self.account_key.
    candidates = _store(config).list_trend_candidates(limit=limit, status=status)
    return 0, {
        "ok": True,
        "account": config.name,
        "writes": 0,
        "count": len(candidates),
        "candidates": candidates,
    }


def _run_trend_show(config: AccountConfig, *, candidate_id: int) -> tuple[int, dict[str, Any]]:
    row = _store(config).get_trend_candidate(candidate_id)
    return 0, {
        "ok": True,
        "account": config.name,
        "writes": 0,
        "found": row is not None,
        "candidate": row,
    }


def _run_trend_enrich(
    config: AccountConfig,
    *,
    candidate_id: int | None,
    url: str | None,
    dry_run: bool,
    settle: float,
) -> tuple[int, dict[str, Any]]:
    """Deterministic read-only browser enrichment (v1: source_post_id only).

    Dry-run NEVER constructs a store; live mode only PATCHs the single
    allowlisted factual field through the account-scoped store method.
    """
    from .trend_enrich import enrich_candidate, enrich_permalink_dry_run

    profile_raw = config.get("THREADS_BROWSER_PROFILE", "") or ""
    if not profile_raw:
        raise AccountConfigError(
            "THREADS_BROWSER_PROFILE is required for trend enrichment")
    profile_dir = Path(profile_raw).expanduser()

    if dry_run:
        if not url:
            raise AccountConfigError("--url is required with --dry-run")
        permalink, _username = normalize_threads_post_url(url)
        result = enrich_permalink_dry_run(
            permalink=permalink, profile_dir=profile_dir, settle_seconds=settle)
        return 0, {"ok": True, "account": config.name, **result}

    if candidate_id is None:
        raise AccountConfigError("--id is required for a live enrich")
    result = enrich_candidate(
        _store(config),
        candidate_id=candidate_id,
        profile_dir=profile_dir,
        settle_seconds=settle,
    )
    return 0, {
        "ok": True,
        "account": config.name,
        "dry_run": False,
        "writes": 1 if result["updated"] else 0,
        **result,
    }


def main(
    argv: list[str] | None = None,
    *,
    process_env: Mapping[str, str] | None = None,
) -> int:
    env = process_env if process_env is not None else os.environ
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # invalid flags/choices: fail closed, no writes
        return int(exc.code or 2)

    if args.command == "accounts" and args.accounts_command == "list":
        for account in list_accounts(env):
            print(account)
        return 0

    config: AccountConfig | None = None
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
        elif args.command == "enqueue-draft":
            code, payload = _run_enqueue_draft(
                config,
                text=args.text,
                replies=args.reply,
                campaign_code=args.campaign_code,
                scheduled_at=args.scheduled_at,
            )
        elif args.command == "publish":
            code, payload = _run_publish(config, dry_run=args.dry_run)
        elif args.command == "trend" and args.trend_command == "add":
            code, payload = _run_trend_add(
                config,
                url=args.url,
                text=args.text,
                username=args.username,
                dry_run=args.dry_run,
            )
        elif args.command == "trend" and args.trend_command == "list":
            code, payload = _run_trend_list(
                config, limit=args.limit, status=args.status
            )
        elif args.command == "trend" and args.trend_command == "show":
            code, payload = _run_trend_show(config, candidate_id=args.id)
        elif args.command == "trend" and args.trend_command == "enrich":
            code, payload = _run_trend_enrich(
                config,
                candidate_id=args.id,
                url=args.url,
                dry_run=args.dry_run,
                settle=args.settle,
            )
        else:
            raise RuntimeError(f"Unsupported command: {args.command}")
    except AccountConfigError as exc:
        print(redact_error(exc, _known_secrets(config)), file=sys.stderr)
        return 2
    except Exception as exc:
        safe_error = redact_error(
            f"{type(exc).__name__}: {exc}", _known_secrets(config)
        )
        print(
            json.dumps(
                {"ok": False, "error": safe_error},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1

    payload = _sanitize_payload(payload, config)
    serialized = json.dumps(payload, separators=(",", ":"))
    if code == 3:
        print(payload.get("error", serialized), file=sys.stderr)
    else:
        print(serialized)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
