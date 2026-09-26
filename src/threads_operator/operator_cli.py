"""Unified account-aware CLI for Threads Operator deployments."""
from __future__ import annotations

import argparse
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
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
from .engagement_queue import (
    ENGAGEMENT_STATUSES,
    approve_reply,
    claim_approved_reply,
    edit_pending_reply,
    list_actions,
    mark_reply_failed,
    mark_reply_posted,
    propose_reply,
    reject_reply,
)
from .publish_worker import publish_next_with_recovery
from .publisher import publish_next
from .safe_errors import redact_error
from .scheduling import (
    SchedulingConfig,
    _parse_ts,
    recommend_slot,
)
from .supabase_store import SupabaseStore, TREND_STATUSES, trend_candidate_payload
from .threads_api import DEFAULT_BASE_URL, ThreadsAPI
from .trend_urls import normalize_threads_post_url

logger = logging.getLogger(__name__)

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
    enqueue.add_argument(
        "--topic",
        help="Meaningful topic for this content (published as the Meta topic_tag).",
    )

    publish = sub.add_parser("publish", help="Publish one approved due queue item")
    publish.add_argument("--account")
    publish.add_argument("--dry-run", action="store_true")

    worker = sub.add_parser(
        "publish-worker",
        help=(
            "Cron-friendly publish worker: publishes the oldest due approved row "
            "and automatically requeues transient failures to approved for the "
            "next scheduled tick. Non-recoverable errors stay failed."
        ),
    )
    worker.add_argument("--account")
    worker.add_argument("--campaign-code")
    worker.add_argument("--dry-run", action="store_true")


    engagement = sub.add_parser(
        "engagement", help="Manage approval-gated engagement actions"
    )
    engagement_sub = engagement.add_subparsers(
        dest="engagement_command", required=True
    )

    engagement_propose = engagement_sub.add_parser(
        "propose-reply",
        help="Queue a Hermes-generated reply for explicit Telebot approval",
    )
    engagement_propose.add_argument("--account")
    engagement_propose.add_argument("--source-post-id", required=True)
    engagement_propose.add_argument("--url", required=True)
    engagement_propose.add_argument("--text", required=True)
    engagement_propose.add_argument("--username")
    engagement_propose.add_argument("--source-text")
    engagement_propose.add_argument("--candidate-id", type=int)
    engagement_propose.add_argument("--score", type=float)
    engagement_propose.add_argument("--reason")
    engagement_propose.add_argument("--expires-at")

    engagement_list = engagement_sub.add_parser(
        "list", help="List engagement actions for this account"
    )
    engagement_list.add_argument("--account")
    engagement_list.add_argument("--status", choices=sorted(ENGAGEMENT_STATUSES))
    engagement_list.add_argument("--limit", type=int, default=20)

    engagement_approve = engagement_sub.add_parser(
        "approve", help="Approve one pending reply from Telebot"
    )
    engagement_approve.add_argument("--account")
    engagement_approve.add_argument("--id", type=int, required=True)
    engagement_approve.add_argument("--approval-ref")

    engagement_reject = engagement_sub.add_parser(
        "reject", help="Reject one pending reply from Telebot"
    )
    engagement_reject.add_argument("--account")
    engagement_reject.add_argument("--id", type=int, required=True)
    engagement_reject.add_argument("--approval-ref")

    engagement_edit = engagement_sub.add_parser(
        "edit", help="Edit a pending reply without approving it"
    )
    engagement_edit.add_argument("--account")
    engagement_edit.add_argument("--id", type=int, required=True)
    engagement_edit.add_argument("--text", required=True)

    engagement_execute = engagement_sub.add_parser(
        "execute", help="Execute one explicitly approved engagement reply"
    )
    engagement_execute.add_argument("--account")
    engagement_execute.add_argument("--id", type=int, required=True)
    engagement_execute.add_argument("--dry-run", action="store_true")

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
    trend_add.add_argument(
        "--external",
        action="store_true",
        help="Mark this as a Hermes-discovered external trend candidate",
    )
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
    trend_enrich_p.add_argument("--url", help="permalink (dry-run by URL; else use --id)")
    trend_enrich_p.add_argument("--dry-run", action="store_true")
    trend_enrich_p.add_argument("--settle", type=float, default=3.0)

    trendeng = sub.add_parser(
        "trend-engagement",
        help="Workflow A: trend candidate -> original own post (approval-gated)",
    )
    trendeng_sub = trendeng.add_subparsers(dest="trendeng_command", required=True)

    trendeng_draft = trendeng_sub.add_parser("draft")
    trendeng_draft.add_argument("--account")
    trendeng_draft.add_argument("--id", type=int, default=None,
                                help="one specific candidate id (default: all eligible)")
    trendeng_draft.add_argument("--limit", type=int, default=5)
    trendeng_draft.add_argument("--threshold", type=float, default=0.6)
    trendeng_draft.add_argument("--dry-run", action="store_true")

    trendeng_list = trendeng_sub.add_parser("list")
    trendeng_list.add_argument("--account")
    trendeng_list.add_argument("--status", default="pending_approval")
    trendeng_list.add_argument("--limit", type=int, default=20)

    trendeng_approve = trendeng_sub.add_parser("approve")
    trendeng_approve.add_argument("--account")
    trendeng_approve.add_argument("--id", type=int, required=True)
    trendeng_approve.add_argument("--approval-ref", default=None)

    trendeng_edit = trendeng_sub.add_parser("edit")
    trendeng_edit.add_argument("--account")
    trendeng_edit.add_argument("--id", type=int, required=True)
    trendeng_edit.add_argument("--text", required=True)

    trendeng_reject = trendeng_sub.add_parser("reject")
    trendeng_reject.add_argument("--account")
    trendeng_reject.add_argument("--id", type=int, required=True)
    trendeng_reject.add_argument("--approval-ref", default=None)

    trendeng_skip = trendeng_sub.add_parser("skip")
    trendeng_skip.add_argument("--account")
    trendeng_skip.add_argument("--id", type=int, required=True)
    trendeng_skip.add_argument("--approval-ref", default=None)

    trendeng_backlog = trendeng_sub.add_parser(
        "backlog", help="List unused backlog content (approval expired)"
    )
    trendeng_backlog.add_argument("--account")
    trendeng_backlog.add_argument("--limit", type=int, default=10)

    trendeng_timeout = trendeng_sub.add_parser(
        "timeout", help="Move stale pending_approval drafts into backlog"
    )
    trendeng_timeout.add_argument("--account")
    trendeng_timeout.add_argument("--older-than-hours", type=int, default=24)
    trendeng_timeout.add_argument("--dry-run", action="store_true")

    trendeng_telegram_send = trendeng_sub.add_parser(
        "telegram-send",
        help="Send one Telegram approval card for a pending_approval candidate "
             "that has no approval_sent_at yet (legacy recovery)",
    )
    trendeng_telegram_send.add_argument("--account")
    trendeng_telegram_send.add_argument("--id", type=int, required=True)
    trendeng_telegram_send.add_argument("--card", default=None,
                                        help="pre-rendered card text; recomputed from stored draft when omitted")

    trendeng_use = trendeng_sub.add_parser(
        "use", help="Enqueue one backlog item into the publish queue"
    )
    trendeng_use.add_argument("--account")
    trendeng_use.add_argument("--id", type=int, required=True)

    # --- Smart scheduling (Task #3) ---------------------------------------
    # "Approve" already means the content is approved for publication; these
    # subcommands only decide *when* to publish, and always produce an
    # approved + scheduled queue row the existing publish worker can post.
    trendeng_sched_approve = trendeng_sub.add_parser(
        "schedule-approve",
        help="Mark candidate content approved and await a scheduling choice",
    )
    trendeng_sched_approve.add_argument("--account")
    trendeng_sched_approve.add_argument("--id", type=int, required=True)

    trendeng_sched_best = trendeng_sub.add_parser(
        "schedule-best",
        help="Pick the best posting time from historical engagement (with fallback)",
    )
    trendeng_sched_best.add_argument("--account")
    trendeng_sched_best.add_argument("--id", type=int, required=True)

    trendeng_sched_now = trendeng_sub.add_parser(
        "schedule-now", help="Approve the queue row as due immediately"
    )
    trendeng_sched_now.add_argument("--account")
    trendeng_sched_now.add_argument("--id", type=int, required=True)

    trendeng_sched_time = trendeng_sub.add_parser(
        "schedule-time",
        help="Approve the queue row for an explicit (UTC ISO) timestamp",
    )
    trendeng_sched_time.add_argument("--account")
    trendeng_sched_time.add_argument("--id", type=int, required=True)
    trendeng_sched_time.add_argument("--at", required=True, help="UTC ISO timestamp")

    trendeng_sched_cancel = trendeng_sub.add_parser(
        "schedule-cancel",
        help="Cancel scheduling; content stays approved and is NOT published",
    )
    trendeng_sched_cancel.add_argument("--account")
    trendeng_sched_cancel.add_argument("--id", type=int, required=True)

    trendeng_refresh = trendeng_sub.add_parser(
        "refresh", help="Rewrite one backlog draft with the current LLM"
    )
    trendeng_refresh.add_argument("--account")
    trendeng_refresh.add_argument("--id", type=int, required=True)

    trendeng_discard = trendeng_sub.add_parser(
        "discard", help="Mark one backlog item as discarded (kept in DB)"
    )
    trendeng_discard.add_argument("--account")
    trendeng_discard.add_argument("--id", type=int, required=True)

    ownreply = sub.add_parser(
        "own-replies",
        help="Workflow B: replies under our own posts (approval-gated)",
    )
    ownreply_sub = ownreply.add_subparsers(dest="ownreply_command", required=True)

    ownreply_scan = ownreply_sub.add_parser("scan")
    ownreply_scan.add_argument("--account")
    ownreply_scan.add_argument("--limit", type=int, default=10,
                               help="recent own posts to inspect")
    ownreply_scan.add_argument("--propose", action="store_true",
                               help="generate drafts and move discovered -> pending_approval")
    ownreply_scan.add_argument("--dry-run", action="store_true")

    ownreply_list = ownreply_sub.add_parser("list")
    ownreply_list.add_argument("--account")
    ownreply_list.add_argument("--status", default=None)
    ownreply_list.add_argument("--limit", type=int, default=50)

    ownreply_approve = ownreply_sub.add_parser("approve")
    ownreply_approve.add_argument("--account")
    ownreply_approve.add_argument("--id", type=int, required=True)
    ownreply_approve.add_argument("--approval-ref", default=None)

    ownreply_edit = ownreply_sub.add_parser("edit")
    ownreply_edit.add_argument("--account")
    ownreply_edit.add_argument("--id", type=int, required=True)
    ownreply_edit.add_argument("--text", required=True)

    ownreply_reject = ownreply_sub.add_parser("reject")
    ownreply_reject.add_argument("--account")
    ownreply_reject.add_argument("--id", type=int, required=True)
    ownreply_reject.add_argument("--approval-ref", default=None)

    ownreply_ignore = ownreply_sub.add_parser("ignore")
    ownreply_ignore.add_argument("--account")
    ownreply_ignore.add_argument("--id", type=int, required=True)
    ownreply_ignore.add_argument("--approval-ref", default=None)

    ownreply_publish = ownreply_sub.add_parser("publish-approved")
    ownreply_publish.add_argument("--account")
    ownreply_publish.add_argument("--id", type=int, default=None,
                                  help="one specific row id (default: all approved)")
    ownreply_publish.add_argument("--dry-run", action="store_true")

    # --- Task 2C: DM opportunity approval (own-replies dm ...) ---
    ownreply_dm = ownreply_sub.add_parser(
        "dm", help="DM opportunity approval workflow (Task 2C — no DM is sent)"
    )
    ownreply_dm_sub = ownreply_dm.add_subparsers(dest="dm_command", required=True)

    ownreply_dm_draft = ownreply_dm_sub.add_parser(
        "draft", help="generate + persist the DM draft (detected -> drafted)")
    ownreply_dm_draft.add_argument("--account")
    ownreply_dm_draft.add_argument("--id", type=int, required=True)

    ownreply_dm_card = ownreply_dm_sub.add_parser(
        "card", help="render the Telegram approval card text for one opportunity")
    ownreply_dm_card.add_argument("--account")
    ownreply_dm_card.add_argument("--id", type=int, required=True)

    ownreply_dm_approve = ownreply_dm_sub.add_parser("approve")
    ownreply_dm_approve.add_argument("--account")
    ownreply_dm_approve.add_argument("--id", type=int, required=True)
    ownreply_dm_approve.add_argument("--approval-ref", default=None)

    ownreply_dm_edit = ownreply_dm_sub.add_parser("edit")
    ownreply_dm_edit.add_argument("--account")
    ownreply_dm_edit.add_argument("--id", type=int, required=True)
    ownreply_dm_edit.add_argument("--text", required=True)

    ownreply_dm_reject = ownreply_dm_sub.add_parser("reject")
    ownreply_dm_reject.add_argument("--account")
    ownreply_dm_reject.add_argument("--id", type=int, required=True)
    ownreply_dm_reject.add_argument("--approval-ref", default=None)

    ownreply_dm_mark = ownreply_dm_sub.add_parser(
        "mark-card-sent",
        help="stamp the Telegram message ref after the card is delivered")
    ownreply_dm_mark.add_argument("--account")
    ownreply_dm_mark.add_argument("--id", type=int, required=True)
    ownreply_dm_mark.add_argument("--message-ref", required=True)

    ownreply_dm_needing = ownreply_dm_sub.add_parser(
        "needing-card", help="list opportunities the watchdog must act on")
    ownreply_dm_needing.add_argument("--account")
    ownreply_dm_needing.add_argument("--limit", type=int, default=50)

    # --- Task 2D: browser DM send (consumes only approved) ---
    ownreply_dm_send = ownreply_dm_sub.add_parser(
        "send", help="claim + browser-send ONE approved DM opportunity by id")
    ownreply_dm_send.add_argument("--account")
    ownreply_dm_send.add_argument("--id", type=int, required=True)
    ownreply_dm_send.add_argument("--worker-id", default=None)

    ownreply_dm_next = ownreply_dm_sub.add_parser(
        "send-next", help="claim + browser-send the next approved opportunity")
    ownreply_dm_next.add_argument("--account")
    ownreply_dm_next.add_argument("--worker-id", default=None)

    ownreply_dm_list = ownreply_dm_sub.add_parser(
        "send-queue", help="list approved / sending / send_uncertain opportunities")
    ownreply_dm_list.add_argument("--account")
    ownreply_dm_list.add_argument("--limit", type=int, default=50)

    ownreply_dm_inspect = ownreply_dm_sub.add_parser(
        "inspect", help="show the send state of one opportunity")
    ownreply_dm_inspect.add_argument("--account")
    ownreply_dm_inspect.add_argument("--id", type=int, required=True)

    ownreply_dm_reconcile = ownreply_dm_sub.add_parser(
        "reconcile", help="resolve a send_uncertain opportunity via the browser")
    ownreply_dm_reconcile.add_argument("--account")
    ownreply_dm_reconcile.add_argument("--id", type=int, required=True)

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
    checks["engagement_enabled"] = config.get_bool("THREADS_ENGAGEMENT_ENABLED")

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
    topic: str | None = None,
) -> tuple[int, dict[str, Any]]:
    table = config.get("THREADS_QUEUE_TABLE", "threads_publish_queue") or "threads_publish_queue"
    campaign = campaign_code or config.get("THREADS_QUEUE_CAMPAIGN_CODE", "") or None
    row = _store(config).enqueue_draft(
        table,
        text,
        reply_texts=replies,
        campaign_code=campaign,
        scheduled_at=scheduled_at,
        topic=topic,
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


def _run_publish_worker(
    config: AccountConfig,
    *,
    campaign_code: str | None,
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
    campaign = campaign_code or config.get("THREADS_QUEUE_CAMPAIGN_CODE", "") or None
    result = publish_next_with_recovery(
        _api(config),
        _store(config),
        table,
        campaign_code=campaign,
        dry_run=dry_run,
    )
    result = {"account": config.name, **result}
    # A transient failure that was requeued is recoverable — the next tick will
    # retry it — so do not signal a hard failure to the caller. A requeue that
    # could not be persisted is a hard failure (the row stays failed).
    failed = result.get("status") == "failed"
    recovered = bool(result.get("requeued"))
    return (1 if failed and not recovered else 0), result


def _run_engagement_propose_reply(
    config: AccountConfig,
    *,
    source_post_id: str,
    url: str,
    text: str,
    username: str | None,
    source_text: str | None,
    candidate_id: int | None,
    score: float | None,
    reason: str | None,
    expires_at: str | None,
) -> tuple[int, dict[str, Any]]:
    result = propose_reply(
        _store(config),
        source_post_id=source_post_id,
        source_permalink=url,
        proposed_text=text,
        source_username=username,
        source_text=source_text,
        trend_candidate_id=candidate_id,
        score=score,
        reason=reason,
        expires_at=expires_at,
    )
    return 0, {"ok": True, "account": config.name, **result}


def _run_engagement_list(
    config: AccountConfig, *, status: str | None, limit: int
) -> tuple[int, dict[str, Any]]:
    rows = list_actions(_store(config), status=status, limit=limit)
    return 0, {
        "ok": True,
        "account": config.name,
        "count": len(rows),
        "actions": rows,
    }


def _run_engagement_approve(
    config: AccountConfig, *, engagement_id: int, approval_ref: str | None
) -> tuple[int, dict[str, Any]]:
    row = approve_reply(_store(config), engagement_id, approval_ref=approval_ref)
    if row is None:
        return 2, {
            "ok": False,
            "account": config.name,
            "error": "Reply is not pending approval or does not belong to this account",
        }
    return 0, {"ok": True, "account": config.name, "action": row}


def _run_engagement_reject(
    config: AccountConfig, *, engagement_id: int, approval_ref: str | None
) -> tuple[int, dict[str, Any]]:
    row = reject_reply(_store(config), engagement_id, approval_ref=approval_ref)
    if row is None:
        return 2, {
            "ok": False,
            "account": config.name,
            "error": "Reply is not pending approval or does not belong to this account",
        }
    return 0, {"ok": True, "account": config.name, "action": row}


def _run_engagement_edit(
    config: AccountConfig, *, engagement_id: int, text: str
) -> tuple[int, dict[str, Any]]:
    row = edit_pending_reply(_store(config), engagement_id, proposed_text=text)
    if row is None:
        return 2, {
            "ok": False,
            "account": config.name,
            "error": "Reply is not pending approval or does not belong to this account",
        }
    return 0, {"ok": True, "account": config.name, "action": row}


def _run_engagement_execute(
    config: AccountConfig, *, engagement_id: int, dry_run: bool
) -> tuple[int, dict[str, Any]]:
    store = _store(config)
    rows = list_actions(store, status="approved", limit=100)
    target = next((row for row in rows if row.get("id") == engagement_id), None)
    if dry_run:
        return 0, {
            "ok": True,
            "account": config.name,
            "dry_run": True,
            "eligible": target is not None,
            "action": target,
        }
    if not config.get_bool("THREADS_ENGAGEMENT_ENABLED"):
        return 3, {
            "ok": False,
            "account": config.name,
            "error": "Live engagement is disabled; require THREADS_ENGAGEMENT_ENABLED=true",
        }

    row = claim_approved_reply(store, engagement_id)
    if row is None:
        return 2, {
            "ok": False,
            "account": config.name,
            "error": "Reply is not approved or does not belong to this account",
        }
    if row.get("action") != "reply":
        mark_reply_failed(store, engagement_id, error="Only reply execution is enabled")
        return 2, {
            "ok": False,
            "account": config.name,
            "error": "Only reply execution is enabled",
        }

    # Validate the target post is a real Graph API media ID before attempting
    # to publish. Trend-scraped IDs are web post IDs that the Graph API does
    # not recognise; without this check the row would burn to `failed`.
    reply_to_id = str(row.get("source_post_id") or "")
    if reply_to_id:
        try:
            _api(config).get_media(reply_to_id)
        except Exception as exc:
            error = redact_error(
                f"Invalid source_post_id {reply_to_id}: {type(exc).__name__}: {exc}",
                _known_secrets(config),
            )
            return 2, {
                "ok": False,
                "account": config.name,
                "status": "approved",
                "error": error,
            }

    try:
        reply_id = _api(config).publish_text(
            str(row.get("proposed_text") or ""),
            reply_to_id=str(row.get("source_post_id") or ""),
        )
        posted = mark_reply_posted(
            store, engagement_id, external_action_id=reply_id
        )
        return 0, {
            "ok": True,
            "account": config.name,
            "status": "posted",
            "reply_id": reply_id,
            "action": posted,
        }
    except Exception as exc:
        error = redact_error(
            f"{type(exc).__name__}: {exc}", _known_secrets(config)
        )
        mark_reply_failed(store, engagement_id, error=error)
        return 1, {
            "ok": False,
            "account": config.name,
            "status": "failed",
            "error": error,
        }


def _run_trend_add(
    config: AccountConfig,
    *,
    url: str,
    text: str | None,
    username: str | None,
    external: bool,
    dry_run: bool,
) -> tuple[int, dict[str, Any]]:
    # The selected --account is authoritative: target_account_id always comes
    # from config.name; there is no flag to override it.
    candidate_role = "external_trend" if external else "manual_ingress"
    external_metadata = (
        {
            "manual": False,
            "discovery_method": "hermes_external",
            "candidate_roles": ["external_trend"],
            "discovery_methods": ["hermes_external"],
            "discovered_by": "hermes",
        }
        if external
        else None
    )
    candidate = trend_candidate_payload(
        config.name,
        url,
        candidate_role=candidate_role,
        source_username=username,
        source_text=text,
        raw_metadata=external_metadata,
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
        url,
        candidate_role=candidate_role,
        source_username=username,
        source_text=text,
        raw_metadata=external_metadata,
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
    """Deterministic read-only browser enrichment (v2: allowlisted evidence).

    Dry-run NEVER writes; with --id it may construct a read-only store for
    the account-scoped candidate read. Live mode PATCHes only allowlisted
    factual evidence through the account-scoped store method.
    """
    from .trend_enrich import (
        enrich_candidate,
        enrich_candidate_dry_run,
        enrich_permalink_dry_run,
    )

    profile_raw = config.get("THREADS_BROWSER_PROFILE", "") or ""
    if not profile_raw:
        raise AccountConfigError(
            "THREADS_BROWSER_PROFILE is required for trend enrichment")
    profile_dir = Path(profile_raw).expanduser()

    if dry_run:
        if url:
            permalink, _username = normalize_threads_post_url(url)
            result = enrich_permalink_dry_run(
                permalink=permalink, profile_dir=profile_dir, settle_seconds=settle)
            return 0, {"ok": True, "account": config.name, **result}
        if candidate_id is None:
            raise AccountConfigError(
                "--url or --id is required with --dry-run")
        result = enrich_candidate_dry_run(
            _store(config),
            candidate_id=candidate_id,
            profile_dir=profile_dir,
            settle_seconds=settle,
        )
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


# --------------------------------------------------------------------------
# Workflow A: trend candidate -> original own post
# --------------------------------------------------------------------------

def _personas_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "personas"


def _send_trend_approval_card(
    *,
    token: str,
    chat_id: str,
    candidate_id: int,
    card: str,
) -> str | None:
    """Send one Telegram approval card for a trend candidate.

    Returns ``"<chat_id>:<message_id>"`` on confirmed delivery, else None.
    Raises on HTTP/network failure so the caller can keep the row retryable.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": card,
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "Approve", "callback_data": f"trendeng:approve:{candidate_id}"},
                    {"text": "Edit", "callback_data": f"trendeng:edit:{candidate_id}"},
                ],
                [
                    {"text": "Reject", "callback_data": f"trendeng:reject:{candidate_id}"},
                    {"text": "Skip", "callback_data": f"trendeng:skip:{candidate_id}"},
                ],
            ]
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok"):
        desc = body.get("description") or "unknown telegram error"
        raise RuntimeError(f"telegram api not ok: {desc}")
    result = body.get("result") or {}
    message_id = result.get("message_id")
    if message_id is None:
        return None
    chat = result.get("chat") or {}
    return f"{chat.get('id') or chat_id}:{message_id}"


def _run_trendeng_draft(
    config: AccountConfig,
    *,
    candidate_id: int | None,
    limit: int,
    threshold: float,
    dry_run: bool,
) -> tuple[int, dict[str, Any]]:
    from . import trend_engagement

    store = _store(config)
    persona_text = trend_engagement.load_persona(_personas_root(), config.name)

    if candidate_id is not None:
        row = store.get_trend_candidate(candidate_id)
        if row is None:
            return 2, {"ok": False, "account": config.name,
                       "error": f"candidate {candidate_id} not found for this account"}
        candidates = [row]
    else:
        seen: dict[int, dict[str, Any]] = {}
        for status in ("discovered", "drafted"):
            for row in store.list_trend_candidates(limit=min(100, max(limit * 4, 20)), status=status):
                seen[int(row["id"])] = row
        candidates = list(seen.values())[:limit]

    drafted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for candidate in candidates:
        cid = int(candidate["id"])
        try:
            verdict = trend_engagement.score_candidate(
                candidate,
                persona_text=persona_text,
                threshold=threshold,
            )
            if not verdict["relevant"]:
                if not dry_run and candidate.get("status") == "discovered":
                    store.update_trend_candidate_workflow_a(
                        candidate_id=cid,
                        fields={
                            "status": "skipped",
                            "trend_score": verdict["score"],
                            "why_it_works": verdict.get("reason") or "below relevance threshold",
                        },
                    )
                skipped.append({"id": cid, "score": verdict["score"],
                                "reason": verdict.get("reason")})
                continue
            draft = trend_engagement.generate_original_draft(
                candidate, verdict, persona_text=persona_text
            )
            proposal = {
                "draft_text": draft,
                "topic": verdict.get("topic"),
                "reason": verdict.get("reason"),
                "angle": verdict.get("angle"),
                "relevance_score": verdict["score"],
            }
            if dry_run:
                drafted.append({"id": cid, "dry_run": True, **proposal})
                continue
            fields = trend_engagement.trend_candidate_payload(candidate, verdict, draft)
            from_status = candidate.get("status") or "discovered"
            if from_status not in ("discovered", "drafted"):
                errors.append({"id": cid, "error": f"unexpected status {from_status}"})
                continue
            telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
            telegram_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
            telegram_mode = bool(telegram_token and telegram_chat)
            card_body = trend_engagement.card_text(candidate, proposal)
            if not telegram_mode:
                # No Telegram creds: persist the draft but DO NOT start the
                # approval timer. discovered -> drafted keeps the row retryable;
                # drafted -> drafted CAS will not match, so refresh fields directly.
                if from_status == "discovered":
                    updated = store.transition_trend_candidate(
                        candidate_id=cid,
                        from_status="discovered",
                        to_status="drafted",
                        fields={k: v for k, v in fields.items() if k != "status"},
                    )
                else:
                    updated = store.update_trend_candidate_workflow_a(
                        candidate_id=cid,
                        fields={k: v for k, v in fields.items() if k != "status"},
                    )
                if updated is None:
                    errors.append({"id": cid, "error": "status precondition failed (already moved)"})
                    continue
                drafted.append({"id": cid, **proposal, "card": card_body,
                                "status": "drafted",
                                "note": "telegram creds missing; card not sent"})
                continue
            # 1. Persist draft fields while the row stays discovered/drafted.
            if from_status == "discovered":
                staged = store.transition_trend_candidate(
                    candidate_id=cid,
                    from_status="discovered",
                    to_status="drafted",
                    fields={k: v for k, v in fields.items() if k != "status"},
                )
            else:
                staged = store.update_trend_candidate_workflow_a(
                    candidate_id=cid,
                    fields={k: v for k, v in fields.items() if k != "status"},
                )
            if staged is None:
                errors.append({"id": cid, "error": "status precondition failed (already moved)"})
                continue
            # 2. Claim approval_sent_at BEFORE sending. Only the worker that
            #    wins this CAS may send a card — duplicates are impossible.
            claimed = store.mark_trend_candidate_approval_sent(candidate_id=cid)
            if claimed is None:
                errors.append({"id": cid, "error": "approval claim lost to concurrent worker; card not sent"})
                continue
            # 3. Send the card; on failure release the claim so it stays retryable.
            try:
                message_ref = _send_trend_approval_card(
                    token=telegram_token,
                    chat_id=telegram_chat,
                    candidate_id=cid,
                    card=card_body,
                )
            except Exception as exc:  # noqa: BLE001 - one candidate must not kill the batch
                store.update_trend_candidate_workflow_a(
                    candidate_id=cid, fields={"approval_sent_at": None},
                )
                errors.append({"id": cid, "error": f"telegram send failed: {type(exc).__name__}: {exc}"})
                continue
            # 4. Card delivered: promote drafted -> pending_approval and carry
            #    the stamped approval_sent_at + message ref forward.
            sent_at = claimed.get("approval_sent_at")
            promoted = store.transition_trend_candidate(
                candidate_id=cid,
                from_status="drafted",
                to_status="pending_approval",
                fields={"approval_sent_at": sent_at},
            )
            if promoted is None:
                errors.append({"id": cid, "error": "promotion to pending_approval failed after card delivery"})
                continue
            if message_ref:
                store._merge_trend_candidate_approval_ref(
                    candidate_id=cid,
                    account_key=config.name,
                    approval_ref=message_ref,
                )
            drafted.append({**({"id": cid, "telegram_message_ref": message_ref} if message_ref else {"id": cid}),
                            **proposal,
                            "approval_sent_at": sent_at,
                            "card": card_body})
        except Exception as exc:  # noqa: BLE001 - one candidate must not kill the batch
            errors.append({"id": cid, "error": f"{type(exc).__name__}: {exc}"})

    return 0, {
        "ok": True,
        "account": config.name,
        "dry_run": dry_run,
        "drafted_count": len(drafted),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "drafted": drafted,
        "skipped": skipped,
        "errors": errors,
        "telegram_sent": bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")),
    }


def _run_trendeng_list(
    config: AccountConfig, *, status: str | None, limit: int
) -> tuple[int, dict[str, Any]]:
    store = _store(config)
    rows = store.list_trend_candidates(limit=limit, status=status or None)
    out = []
    for row in rows:
        wa = (row.get("raw_metadata") or {}).get("workflow_a") or {}
        out.append({
            "id": row.get("id"),
            "status": row.get("status"),
            "source_username": row.get("source_username"),
            "source_permalink": row.get("source_permalink"),
            "topic": row.get("topic"),
            "trend_score": row.get("trend_score"),
            "draft_text": wa.get("draft_text"),
            "reason": wa.get("reason"),
            "used_in_queue_id": row.get("used_in_queue_id"),
        })
    return 0, {"ok": True, "account": config.name, "count": len(out), "candidates": out}


def _trendeng_set_status(
    config: AccountConfig,
    *,
    candidate_id: int,
    from_status: str,
    to_status: str,
) -> tuple[int, dict[str, Any]]:
    store = _store(config)
    row = store.transition_trend_candidate(
        candidate_id=candidate_id, from_status=from_status, to_status=to_status
    )
    if row is None:
        return 2, {"ok": False, "account": config.name,
                   "error": f"candidate {candidate_id} is not {from_status} (or not this account)"}
    return 0, {"ok": True, "account": config.name, "id": candidate_id, "status": to_status}


# ---------------------------------------------------------------------------
# Smart scheduling (Task #3)
#
# "Approve" already means the content is approved for publication. These
# helpers only decide *when* to publish and always leave the queue row in the
# terminal state ``approved`` + ``scheduled_at`` so the existing publish worker
# picks it up. Idempotency: a candidate's existing ``used_in_queue_id`` is
# reused (promoted from draft) instead of inserting a duplicate row.
# ---------------------------------------------------------------------------


def _scheduling_config(config: AccountConfig) -> SchedulingConfig:
    def _int(key: str, default: int) -> int:
        raw = (config.get(key) or "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            return default
        return value if value > 0 else default

    hours_raw = (config.get("THREADS_SCHED_FALLBACK_HOURS") or "").strip()
    hours = SchedulingConfig().fallback_hours_local
    if hours_raw:
        parsed: list[int] = []
        for part in hours_raw.split(","):
            part = part.strip()
            if part.isdigit() and 0 <= int(part) <= 23:
                parsed.append(int(part))
        if parsed:
            hours = tuple(parsed)
    return SchedulingConfig(
        min_gap_minutes=_int("THREADS_SCHED_MIN_GAP_MINUTES", 60),
        min_samples=_int("THREADS_SCHED_MIN_SAMPLES", 2),
        lookahead_days=_int("THREADS_SCHED_LOOKAHEAD_DAYS", 7),
        min_lead_minutes=_int("THREADS_SCHED_MIN_LEAD_MINUTES", 5),
        fallback_hours_local=hours,
        history_window_days=_int("THREADS_SCHED_HISTORY_WINDOW_DAYS", 90),
    )


def _candidate_draft(candidate: dict[str, Any]) -> tuple[str, str | None]:
    wa = (candidate.get("raw_metadata") or {}).get("workflow_a") or {}
    draft = (wa.get("draft_text") or "").strip()
    topic = (candidate.get("topic") or wa.get("topic") or "").strip() or None
    return draft, topic


def _ensure_approved_queue_row(
    store: SupabaseStore,
    config: AccountConfig,
    candidate: dict[str, Any],
    *,
    scheduled_utc,
) -> dict[str, Any]:
    """Return an approved + scheduled queue row for ``candidate``.

    Reuses (promotes) an existing draft queue row referenced by
    ``used_in_queue_id``; otherwise inserts a fresh approved row. Never returns
    a draft — callers can rely on the row being publishable.
    """
    scheduled_at = scheduled_utc.isoformat()
    table = config.get("THREADS_QUEUE_TABLE", "threads_publish_queue") or "threads_publish_queue"
    campaign = config.get("THREADS_QUEUE_CAMPAIGN_CODE", "") or None

    existing_id = candidate.get("used_in_queue_id")
    if existing_id:
        promoted = store.promote_draft_to_approved(table, existing_id, scheduled_at=scheduled_at)
        if promoted is not None:
            logger.info(
                "schedule: promoted existing queue row #%s to approved @ %s",
                existing_id, scheduled_at,
            )
            return promoted
        current = store.fetch_queue_row(table, existing_id)
        if current and current.get("status") == "approved":
            store._patch_queue_row(table, existing_id, {"scheduled_at": scheduled_at})
            current["scheduled_at"] = scheduled_at
            logger.info("schedule: reused approved queue row #%s", existing_id)
            return current
        # Row was posted/failed/missing — fall through and create a fresh row.
        logger.info(
            "schedule: linked queue row #%s not reusable (status=%s); creating new",
            existing_id, (current or {}).get("status"),
        )

    draft, topic = _candidate_draft(candidate)
    row = store.enqueue_approved(
        table, draft, reply_texts=[], campaign_code=campaign,
        scheduled_at=scheduled_at, topic=topic,
    )
    logger.info("schedule: created approved queue row #%s @ %s", row.get("id"), scheduled_at)
    return row


def _finalize_scheduled_candidate(
    store: SupabaseStore, *, candidate_id: int, queue_id: int
) -> str:
    finished = store.update_trend_candidate_workflow_a(
        candidate_id=candidate_id,
        fields={"status": "queued", "used_in_queue_id": int(queue_id)},
    )
    return (finished or {}).get("status", "queued")


def _run_trendeng_schedule_approve(
    config: AccountConfig, *, candidate_id: int
) -> tuple[int, dict[str, Any]]:
    """Content approval only — CAS pending_approval -> approved, await scheduling."""
    store = _store(config)
    candidate = store.get_trend_candidate(candidate_id)
    if candidate is None:
        return 2, {"ok": False, "account": config.name,
                   "error": f"candidate {candidate_id} not found for this account"}
    if candidate.get("status") == "approved":
        return 0, {"ok": True, "account": config.name, "id": candidate_id,
                   "status": "approved", "already_approved": True}
    if candidate.get("status") != "pending_approval":
        return 2, {"ok": False, "account": config.name,
                   "error": f"candidate is {candidate.get('status')}, not pending_approval"}
    draft, topic = _candidate_draft(candidate)
    if not draft:
        return 2, {"ok": False, "account": config.name,
                   "error": "no draft text on candidate — redraft before approval"}
    moved = store.transition_trend_candidate(
        candidate_id=candidate_id, from_status="pending_approval", to_status="approved"
    )
    if moved is None:
        current = store.get_trend_candidate(candidate_id) or {}
        return 0, {"ok": True, "account": config.name, "id": candidate_id,
                   "status": current.get("status"),
                   "already_approved": current.get("status") == "approved"}
    logger.info("schedule: candidate #%d content approved; awaiting scheduling choice", candidate_id)
    return 0, {"ok": True, "account": config.name, "id": candidate_id,
               "status": "approved", "topic": topic}


def _load_schedulable_candidate(
    config: AccountConfig, candidate_id: int
) -> tuple[SupabaseStore, dict[str, Any] | None, dict[str, Any] | None]:
    store = _store(config)
    candidate = store.get_trend_candidate(candidate_id)
    if candidate is None:
        return store, None, {"ok": False, "account": config.name,
                             "error": f"candidate {candidate_id} not found for this account"}
    if candidate.get("status") == "queued" and candidate.get("used_in_queue_id"):
        qid = candidate["used_in_queue_id"]
        table = config.get("THREADS_QUEUE_TABLE", "threads_publish_queue") or "threads_publish_queue"
        row = store.fetch_queue_row(table, qid) or {}
        return store, None, {
            "ok": True, "account": config.name, "id": candidate_id,
            "status": "queued", "queue_id": qid,
            "queue_status": row.get("status"),
            "scheduled_at": row.get("scheduled_at"),
            "queue_row": row or None,
            "already_queued": True,
        }
    if candidate.get("status") != "approved":
        return store, None, {"ok": False, "account": config.name,
                             "error": f"candidate is {candidate.get('status')}, not approved (approve first)"}
    draft, _topic = _candidate_draft(candidate)
    if not draft:
        return store, None, {"ok": False, "account": config.name,
                             "error": "no draft text on candidate — redraft before scheduling"}
    return store, candidate, None


def _run_trendeng_schedule_best(
    config: AccountConfig, *, candidate_id: int
) -> tuple[int, dict[str, Any]]:
    store, candidate, err = _load_schedulable_candidate(config, candidate_id)
    if err is not None:
        return (0 if err.get("already_queued") else 2), err
    assert candidate is not None
    cfg = _scheduling_config(config)
    now_utc = datetime.now(timezone.utc)
    since = (now_utc - timedelta(days=cfg.history_window_days)).isoformat()
    try:
        insight_rows = store.list_post_insight_rows(since_utc=since, limit=cfg.max_insight_rows)
    except Exception:  # noqa: BLE001
        logger.warning("schedule: insight history unavailable; using fallback", exc_info=True)
        insight_rows = []
    occupied = []
    try:
        rows = store.list_scheduled_queue_rows(
            config.get("THREADS_QUEUE_TABLE", "threads_publish_queue") or "threads_publish_queue",
            from_utc=now_utc.isoformat(),
        )
        occupied = [ts for ts in (_parse_ts(r.get("scheduled_at")) for r in rows) if ts]
    except Exception:  # noqa: BLE001
        logger.warning("schedule: queue occupancy unavailable; scheduling without collision data",
                       exc_info=True)
    rec = recommend_slot(insight_rows, occupied, now=now_utc, config=cfg)
    logger.info(
        "schedule: candidate #%d best-time source=%s slot=%s score=%.4f n=%d reason=%s",
        candidate_id, rec.source, rec.scheduled_utc.isoformat(), rec.score,
        rec.sample_size, rec.reason,
    )
    row = _ensure_approved_queue_row(store, config, candidate, scheduled_utc=rec.scheduled_utc)
    status = _finalize_scheduled_candidate(store, candidate_id=candidate_id, queue_id=int(row["id"]))
    return 0, {
        "ok": True, "account": config.name, "id": candidate_id, "status": status,
        "queue_id": row.get("id"), "queue_status": "approved",
        "scheduled_at": row.get("scheduled_at") or rec.scheduled_utc.isoformat(),
        "schedule_source": rec.source,
        "score": rec.score, "sample_size": rec.sample_size, "reason": rec.reason,
        "queue_row": row,
    }


def _run_trendeng_schedule_now(
    config: AccountConfig, *, candidate_id: int
) -> tuple[int, dict[str, Any]]:
    store, candidate, err = _load_schedulable_candidate(config, candidate_id)
    if err is not None:
        return (0 if err.get("already_queued") else 2), err
    assert candidate is not None
    now_utc = datetime.now(timezone.utc)
    logger.info("schedule: candidate #%d post-now @ %s", candidate_id, now_utc.isoformat())
    row = _ensure_approved_queue_row(store, config, candidate, scheduled_utc=now_utc)
    status = _finalize_scheduled_candidate(store, candidate_id=candidate_id, queue_id=int(row["id"]))
    return 0, {
        "ok": True, "account": config.name, "id": candidate_id, "status": status,
        "queue_id": row.get("id"), "queue_status": "approved",
        "scheduled_at": row.get("scheduled_at") or now_utc.isoformat(),
        "schedule_source": "now", "queue_row": row,
    }


def _run_trendeng_schedule_time(
    config: AccountConfig, *, candidate_id: int, at: str
) -> tuple[int, dict[str, Any]]:
    store, candidate, err = _load_schedulable_candidate(config, candidate_id)
    if err is not None:
        return (0 if err.get("already_queued") else 2), err
    assert candidate is not None
    ts = _parse_ts(at)
    if ts is None:
        return 2, {"ok": False, "account": config.name, "error": f"invalid --at timestamp: {at!r}"}
    logger.info("schedule: candidate #%d custom time @ %s", candidate_id, ts.isoformat())
    row = _ensure_approved_queue_row(store, config, candidate, scheduled_utc=ts)
    status = _finalize_scheduled_candidate(store, candidate_id=candidate_id, queue_id=int(row["id"]))
    return 0, {
        "ok": True, "account": config.name, "id": candidate_id, "status": status,
        "queue_id": row.get("id"), "queue_status": "approved",
        "scheduled_at": row.get("scheduled_at") or ts.isoformat(),
        "schedule_source": "custom", "queue_row": row,
    }


def _run_trendeng_schedule_cancel(
    config: AccountConfig, *, candidate_id: int
) -> tuple[int, dict[str, Any]]:
    """Cancel scheduling. Content is NOT published; stays approved, resumable.

    If a draft queue row already exists for this candidate it is removed so no
    stale draft lingers. An already-approved/posted row is left untouched.
    """
    store = _store(config)
    candidate = store.get_trend_candidate(candidate_id)
    if candidate is None:
        return 2, {"ok": False, "account": config.name,
                   "error": f"candidate {candidate_id} not found for this account"}
    status = candidate.get("status")
    qid = candidate.get("used_in_queue_id")
    if status == "queued" and qid:
        return 0, {"ok": True, "account": config.name, "id": candidate_id,
                   "status": "queued", "queue_id": qid,
                   "note": "already scheduled; use the publish queue to change it"}
    if qid:
        table = config.get("THREADS_QUEUE_TABLE", "threads_publish_queue") or "threads_publish_queue"
        row = store.fetch_queue_row(table, qid)
        if row and row.get("status") == "draft":
            store._patch_queue_row(table, qid, {"status": "failed",
                                                "last_error": "scheduling cancelled"})
            logger.info("schedule: cancel removed stale draft queue row #%s", qid)
    logger.info("schedule: candidate #%d scheduling cancelled; content remains approved, not published",
                candidate_id)
    return 0, {"ok": True, "account": config.name, "id": candidate_id,
               "status": status if status in TREND_STATUSES else "approved",
               "cancelled": True}


def _run_trendeng_approve(
    config: AccountConfig, *, candidate_id: int
) -> tuple[int, dict[str, Any]]:
    """Approve = content approved for publication (no queue row created here).

    This handler now ONLY approves the content (CAS pending_approval ->
    approved) and signals the dispatcher to ask *when* to publish. The queue
    row is created as ``approved`` by the subsequent scheduling choice
    (schedule-best / schedule-now / schedule-time). This is the fix for the
    regression where approved content was left stuck as a ``draft`` queue row.
    """
    store = _store(config)
    candidate = store.get_trend_candidate(candidate_id)
    if candidate is None:
        return 2, {"ok": False, "account": config.name,
                   "error": f"candidate {candidate_id} not found for this account"}
    if candidate.get("status") == "queued" and candidate.get("used_in_queue_id"):
        return 0, {
            "ok": True, "account": config.name, "id": candidate_id,
            "status": "queued", "queue_id": candidate.get("used_in_queue_id"),
            "already_queued": True,
        }
    if candidate.get("status") == "approved":
        return 0, {"ok": True, "account": config.name, "id": candidate_id,
                   "status": "approved", "already_approved": True}
    if candidate.get("status") != "pending_approval":
        return 2, {"ok": False, "account": config.name,
                   "error": f"candidate is {candidate.get('status')}, not pending_approval"}

    draft, topic = _candidate_draft(candidate)
    if not draft:
        return 2, {"ok": False, "account": config.name,
                   "error": "no draft text on candidate — redraft before approval"}

    moved = store.transition_trend_candidate(
        candidate_id=candidate_id, from_status="pending_approval", to_status="approved"
    )
    if moved is None:
        current = store.get_trend_candidate(candidate_id) or {}
        return 0, {
            "ok": True, "account": config.name, "id": candidate_id,
            "status": current.get("status"),
            "queue_id": current.get("used_in_queue_id"),
            "already_approved": current.get("status") == "approved",
        }
    logger.info("schedule: candidate #%d approved; prompting for scheduling choice", candidate_id)
    return 0, {
        "ok": True, "account": config.name, "id": candidate_id,
        "status": "approved", "topic": topic, "needs_scheduling": True,
        "draft_preview": draft[:200],
    }


def _run_trendeng_edit(
    config: AccountConfig, *, candidate_id: int, text: str
) -> tuple[int, dict[str, Any]]:
    if len(text) > 500:
        return 2, {"ok": False, "account": config.name,
                   "error": f"edited draft is {len(text)} chars (limit 500)"}
    store = _store(config)
    candidate = store.get_trend_candidate(candidate_id)
    if candidate is None:
        return 2, {"ok": False, "account": config.name,
                   "error": "candidate not found (or not this account)"}
    current_status = candidate.get("status")
    if current_status not in ("pending_approval", "backlog"):
        return 2, {"ok": False, "account": config.name,
                   "error": f"candidate is {current_status}, not pending_approval/backlog"}
    raw = dict(candidate.get("raw_metadata") or {})
    wa = dict(raw.get("workflow_a") or {})
    wa["draft_text"] = text
    wa["edited"] = True
    raw["workflow_a"] = wa
    updated = store.update_trend_candidate_workflow_a(
        candidate_id=candidate_id, fields={"raw_metadata": raw}
    )
    return 0, {"ok": True, "account": config.name, "id": candidate_id,
               "updated": updated is not None, "draft_text": text}


def _run_trendeng_skip(
    config: AccountConfig, *, candidate_id: int
) -> tuple[int, dict[str, Any]]:
    return _trendeng_set_status(
        config,
        candidate_id=candidate_id,
        from_status="pending_approval",
        to_status="skipped",
    )


# --------------------------------------------------------------------------
# Workflow A: approval timeout + content backlog
# --------------------------------------------------------------------------

def _run_trendeng_backlog(
    config: AccountConfig, *, limit: int
) -> tuple[int, dict[str, Any]]:
    store = _store(config)
    rows = store.list_trend_backlog(limit=limit)
    out = []
    for row in rows:
        wa = (row.get("raw_metadata") or {}).get("workflow_a") or {}
        out.append({
            "id": row.get("id"),
            "status": row.get("status"),
            "source_username": row.get("source_username"),
            "source_permalink": row.get("source_permalink"),
            "source_text": row.get("source_text"),
            "topic": row.get("topic"),
            "trend_score": row.get("trend_score"),
            "draft_text": wa.get("draft_text"),
            "reason": wa.get("reason"),
            "angle": wa.get("angle"),
            "approval_sent_at": row.get("approval_sent_at"),
            "timed_out_at": row.get("timed_out_at"),
            "used_in_queue_id": row.get("used_in_queue_id"),
        })
    return 0, {
        "ok": True,
        "account": config.name,
        "count": len(out),
        "items": out,
    }


def _run_trendeng_timeout(
    config: AccountConfig, *, older_than_hours: int, dry_run: bool
) -> tuple[int, dict[str, Any]]:
    store = _store(config)
    stale = store.find_stale_pending_approvals(
        older_than_hours=older_than_hours, limit=100
    )
    if dry_run:
        return 0, {
            "ok": True,
            "account": config.name,
            "dry_run": True,
            "stale_count": len(stale),
            "ids": [row.get("id") for row in stale],
        }
    moved = 0
    errors = []
    for row in stale:
        try:
            result = store.transition_trend_candidate_backlog(
                candidate_id=int(row["id"])
            )
            if result is not None:
                moved += 1
        except Exception as exc:  # noqa: BLE001 - one row must not kill the batch
            errors.append({"id": row.get("id"), "error": str(exc)})
    return 0, {
        "ok": True,
        "account": config.name,
        "moved_to_backlog": moved,
        "errors": errors,
    }


def _run_trendeng_telegram_send(
    config: AccountConfig, *, candidate_id: int, card: str | None
) -> tuple[int, dict[str, Any]]:
    """Deliver one approval card for a pending_approval row that has no
    approval_sent_at yet (legacy recovery), then stamp approval_sent_at.

    Idempotent: rows with a non-null approval_sent_at return ok=True with
    skipped=True without re-sending, so repeat watchdog runs cannot produce
    duplicate cards.
    """
    from . import trend_engagement

    store = _store(config)
    candidate = store.get_trend_candidate(candidate_id)
    if candidate is None:
        return 2, {
            "ok": False,
            "account": config.name,
            "error": f"candidate {candidate_id} not found for this account",
        }
    if candidate.get("status") != "pending_approval":
        return 2, {
            "ok": False,
            "account": config.name,
            "error": f"candidate {candidate_id} is {candidate.get('status')}, not pending_approval",
        }
    if candidate.get("approval_sent_at"):
        return 0, {
            "ok": True,
            "account": config.name,
            "candidate_id": candidate_id,
            "skipped": True,
            "reason": "approval_sent_at already set; duplicate card prevented",
        }
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (telegram_token and telegram_chat):
        return 2, {
            "ok": False,
            "account": config.name,
            "error": "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set",
        }
    card_body = card
    if not card_body:
        wa = (candidate.get("raw_metadata") or {}).get("workflow_a") or {}
        relevance = wa.get("relevance_score")
        if relevance is None:
            relevance = candidate.get("trend_score")
        if relevance is None:
            relevance = 0.0
        proposal = {
            "draft_text": wa.get("draft_text") or "",
            "topic": wa.get("topic") or candidate.get("topic"),
            "reason": wa.get("reason") or candidate.get("why_it_works"),
            "angle": wa.get("angle") or candidate.get("adaptation_angle"),
            "relevance_score": relevance,
        }
        card_body = trend_engagement.card_text(candidate, proposal)
    # Claim BEFORE sending: the CAS on approval_sent_at IS NULL is the single
    # guarantee that no second worker/watchdog run can send a duplicate card.
    claimed = store.mark_trend_candidate_approval_sent(candidate_id=candidate_id)
    if claimed is None:
        return 0, {
            "ok": True,
            "account": config.name,
            "candidate_id": candidate_id,
            "skipped": True,
            "reason": "approval_sent_at claimed concurrently; duplicate card prevented",
        }
    try:
        message_ref = _send_trend_approval_card(
            token=telegram_token,
            chat_id=telegram_chat,
            candidate_id=candidate_id,
            card=card_body,
        )
    except Exception as exc:  # noqa: BLE001
        # Send failed AFTER the claim: release the stamp so a later run can retry.
        store.update_trend_candidate_workflow_a(
            candidate_id=candidate_id, fields={"approval_sent_at": None},
        )
        return 1, {
            "ok": False,
            "account": config.name,
            "candidate_id": candidate_id,
            "error": f"telegram send failed: {type(exc).__name__}: {exc}",
        }
    if message_ref:
        store._merge_trend_candidate_approval_ref(
            candidate_id=candidate_id,
            account_key=config.name,
            approval_ref=message_ref,
        )
    return 0, {
        "ok": True,
        "account": config.name,
        "candidate_id": candidate_id,
        "telegram_message_ref": message_ref,
        "approval_sent_at": claimed.get("approval_sent_at"),
    }


def _run_trendeng_use(
    config: AccountConfig, *, candidate_id: int
) -> tuple[int, dict[str, Any]]:
    """Enqueue one backlog item through the EXISTING enqueue_draft path."""
    store = _store(config)
    candidate = store.get_trend_candidate(candidate_id)
    if candidate is None:
        return 2, {
            "ok": False,
            "account": config.name,
            "error": f"candidate {candidate_id} not found for this account",
        }
    if candidate.get("used_in_queue_id"):
        return 0, {
            "ok": True,
            "account": config.name,
            "id": candidate_id,
            "status": candidate.get("status"),
            "queue_id": candidate.get("used_in_queue_id"),
            "already_queued": True,
        }
    if candidate.get("status") != "backlog":
        return 2, {
            "ok": False,
            "account": config.name,
            "error": f"candidate is {candidate.get('status')}, not backlog",
        }

    wa = (candidate.get("raw_metadata") or {}).get("workflow_a") or {}
    draft = (wa.get("draft_text") or "").strip()
    if not draft:
        return 2, {
            "ok": False,
            "account": config.name,
            "error": "no draft text on candidate — refresh before use",
        }
    if len(draft) > 500:
        return 2, {
            "ok": False,
            "account": config.name,
            "error": f"draft is {len(draft)} chars (limit 500)",
        }
    topic = (candidate.get("topic") or wa.get("topic") or "").strip() or None
    if not topic:
        return 2, {
            "ok": False,
            "account": config.name,
            "error": "no topic on candidate — cannot preserve topic end-to-end",
        }

    # CAS backlog -> approved first so a double-use races here instead of
    # double-scheduling. The queue row itself is created as ``approved`` by the
    # dispatcher's follow-up scheduling choice (schedule-best/now/time), so a
    # backlog item is never left stuck as a draft.
    moved = store.transition_trend_candidate(
        candidate_id=candidate_id, from_status="backlog", to_status="approved"
    )
    if moved is None:
        current = store.get_trend_candidate(candidate_id) or {}
        return 0, {
            "ok": True,
            "account": config.name,
            "id": candidate_id,
            "status": current.get("status"),
            "queue_id": current.get("used_in_queue_id"),
            "already_queued": bool(current.get("used_in_queue_id")),
        }
    logger.info("schedule: backlog candidate #%d approved; prompting for scheduling choice",
                candidate_id)
    return 0, {
        "ok": True,
        "account": config.name,
        "id": candidate_id,
        "status": "approved",
        "topic": topic,
        "needs_scheduling": True,
        "draft_preview": draft[:200],
    }


def _run_trendeng_refresh(
    config: AccountConfig, *, candidate_id: int
) -> tuple[int, dict[str, Any]]:
    """Rewrite one backlog draft with the current configured LLM/persona."""
    from . import trend_engagement

    store = _store(config)
    candidate = store.get_trend_candidate(candidate_id)
    if candidate is None:
        return 2, {
            "ok": False,
            "account": config.name,
            "error": f"candidate {candidate_id} not found for this account",
        }
    if candidate.get("status") != "backlog":
        return 2, {
            "ok": False,
            "account": config.name,
            "error": f"candidate is {candidate.get('status')}, not backlog",
        }

    persona_text = trend_engagement.load_persona(_personas_root(), config.name)
    draft = trend_engagement.refresh_original_draft(
        candidate, persona_text=persona_text
    )
    raw = dict(candidate.get("raw_metadata") or {})
    wa = dict(raw.get("workflow_a") or {})
    wa["draft_text"] = draft
    wa["refreshed_at"] = store._utc_now()
    raw["workflow_a"] = wa
    updated = store.update_trend_candidate_workflow_a(
        candidate_id=candidate_id, fields={"raw_metadata": raw}
    )
    return 0, {
        "ok": True,
        "account": config.name,
        "id": candidate_id,
        "updated": updated is not None,
        "draft_text": draft,
    }


def _run_trendeng_discard(
    config: AccountConfig, *, candidate_id: int
) -> tuple[int, dict[str, Any]]:
    store = _store(config)
    moved = store.transition_trend_candidate(
        candidate_id=candidate_id, from_status="backlog", to_status="discarded"
    )
    if moved is None:
        current = store.get_trend_candidate(candidate_id) or {}
        return 2, {
            "ok": False,
            "account": config.name,
            "error": f"candidate is {current.get('status')}, not backlog",
        }
    return 0, {
        "ok": True,
        "account": config.name,
        "id": candidate_id,
        "status": "discarded",
    }


# --------------------------------------------------------------------------
# Workflow B: replies under our own posts
# --------------------------------------------------------------------------

def _run_ownreply_scan(
    config: AccountConfig, *, limit: int, propose: bool, dry_run: bool
) -> tuple[int, dict[str, Any]]:
    from . import own_replies, trend_engagement

    api = _api(config)
    store = _store(config)

    posts = api.list_posts()[: max(1, min(limit, 50))]
    if dry_run:
        discovered_preview: list[dict[str, Any]] = []
        for post in posts:
            try:
                children = api.list_direct_replies(str(post.get("id") or ""))
            except Exception:
                continue
            for child in children:
                discovered_preview.append({
                    "reply_id": child.get("id"),
                    "parent_post_id": post.get("id"),
                    "from_username": child.get("username"),
                    "reply_text": (child.get("text") or "")[:120],
                })
        return 0, {"ok": True, "account": config.name, "dry_run": True,
                   "posts_scanned": len(posts), "replies_seen": len(discovered_preview),
                   "replies": discovered_preview[:50]}

    me = None
    try:
        me = api.get_authenticated_identity()
    except Exception:
        pass
    own_username = (me or {}).get("username") or ""

    if not propose:
        # No drafting, no DB writes: just report current state.
        discovered_preview: list[dict[str, Any]] = []
        for post in posts:
            try:
                children = api.list_direct_replies(str(post.get("id") or ""))
            except Exception:
                continue
            for child in children:
                discovered_preview.append({
                    "reply_id": child.get("id"),
                    "parent_post_id": post.get("id"),
                    "from_username": child.get("username"),
                    "reply_text": (child.get("text") or "")[:120],
                })
        return 0, {"ok": True, "account": config.name, "propose": False,
                   "posts_scanned": len(posts), "replies_seen": len(discovered_preview),
                   "replies": discovered_preview[:50]}

    new_rows = own_replies.discover_new_replies(
        api=api, store=store, recent_posts=posts, own_username=own_username
    )

    proposed: list[dict[str, Any]] = []
    if propose and new_rows:
        persona_text = trend_engagement.load_persona(_personas_root(), config.name)
        for row in new_rows:
            try:
                # Task 2A/2B live wiring: classify every newly discovered reply
                # before the public-reply approval transition. Classification
                # persists structured context/intent and may idempotently create
                # a DM opportunity; it never sends a DM.
                try:
                    intelligence = own_replies.classify_reply(
                        store=store,
                        account_key=config.name,
                        reply_row=row,
                        persona_text=persona_text,
                    )
                    updated = intelligence.get("updated_row")
                    if isinstance(updated, dict):
                        row = {**row, **updated}
                except Exception as exc:  # noqa: BLE001
                    # Fail closed for the DM/lead path without breaking the
                    # pre-existing public-reply workflow.
                    logger.warning(
                        "reply classification failed for row %s: %s",
                        row.get("id"),
                        exc,
                    )

                draft = own_replies.generate_reply_draft(
                    persona_text=persona_text,
                    parent_post_text=row.get("parent_post_text"),
                    from_username=row.get("from_username"),
                    reply_text=row.get("reply_text"),
                )
                moved = store.transition_own_reply(
                    row_id=int(row["id"]),
                    from_status="discovered",
                    to_status="pending_approval",
                    fields={"proposed_text": draft},
                )
                if moved is not None:
                    proposed.append({"id": moved["id"], "reply_id": moved["reply_id"],
                                     "draft": draft,
                                     "card": own_replies.card_text(moved)})
            except Exception as exc:  # noqa: BLE001
                logger.warning("draft generation failed for row %s: %s", row.get("id"), exc)

    # Task 2A/2B backfill: replies can already be pending_approval from an
    # earlier watchdog tick/deployment while still lacking classification.
    # Classify those rows without re-drafting or re-sending the public-reply
    # approval card. This is idempotent and lets the DM approval pass consume
    # newly-created opportunities in the same watchdog run.
    classified_backfill = 0
    try:
        pending_unclassified = [
            row for row in store.list_own_replies(status="pending_approval", limit=max(10, limit))
            if (
                row.get("classified_at") is None
                or (
                    row.get("dm_opportunity_id") is None
                    and row.get("intent") in {"cta_match", "buying_intent", "potential_lead"}
                )
            )
        ]
        if pending_unclassified:
            if "persona_text" not in locals():
                persona_text = trend_engagement.load_persona(_personas_root(), config.name)
            for row in pending_unclassified:
                try:
                    own_replies.classify_reply(
                        store=store,
                        account_key=config.name,
                        reply_row=row,
                        persona_text=persona_text,
                    )
                    classified_backfill += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "reply classification backfill failed for row %s: %s",
                        row.get("id"),
                        exc,
                    )
    except Exception as exc:  # noqa: BLE001
        logger.warning("reply classification backfill query failed: %s", exc)

    return 0, {
        "ok": True, "account": config.name,
        "posts_scanned": len(posts),
        "new_replies": len(new_rows),
        "proposed": proposed,
        "classified_backfill": classified_backfill,
        "proposed_count": len(proposed),
    }


def _run_ownreply_list(
    config: AccountConfig, *, status: str | None, limit: int
) -> tuple[int, dict[str, Any]]:
    rows = _store(config).list_own_replies(status=status or None, limit=limit)
    return 0, {"ok": True, "account": config.name, "count": len(rows), "replies": rows}


def _run_ownreply_decision(
    config: AccountConfig, *, row_id: int, decision: str
) -> tuple[int, dict[str, Any]]:
    """approve / reject / ignore — all require the row be in an open state."""
    store = _store(config)
    if decision == "approve":
        moved = store.transition_own_reply(
            row_id=row_id, from_status=("discovered", "pending_approval"),
            to_status="approved",
            fields={"approved_at": store._utc_now()},
        )
    elif decision == "reject":
        moved = store.transition_own_reply(
            row_id=row_id, from_status=("discovered", "pending_approval"),
            to_status="rejected",
            fields={"rejected_at": store._utc_now()},
        )
    else:  # ignore
        moved = store.transition_own_reply(
            row_id=row_id, from_status=("discovered", "pending_approval"),
            to_status="ignored",
            fields={"ignored_at": store._utc_now()},
        )
    if moved is None:
        return 2, {"ok": False, "account": config.name,
                   "error": f"row {row_id} is not in an open state (or not this account)"}
    return 0, {"ok": True, "account": config.name, "id": row_id, "status": decision}


def _run_ownreply_edit(
    config: AccountConfig, *, row_id: int, text: str
) -> tuple[int, dict[str, Any]]:
    if len(text) > 500:
        return 2, {"ok": False, "account": config.name,
                   "error": f"edited reply is {len(text)} chars (limit 500)"}
    store = _store(config)
    moved = store.transition_own_reply(
        row_id=row_id, from_status="pending_approval", to_status="pending_approval",
        fields={"proposed_text": text},
    )
    if moved is None:
        return 2, {"ok": False, "account": config.name,
                   "error": f"row {row_id} is not pending_approval (or not this account)"}
    return 0, {"ok": True, "account": config.name, "id": row_id, "proposed_text": text}


# ---------------------------------------------------------------------------
# Task 2C — DM opportunity approval runners (own-replies dm ...)
# ---------------------------------------------------------------------------
#
# These runners own the approval half of the DM lifecycle:
#   detected -> drafted -> awaiting_approval -> approved  (or rejected/expired)
# None of them send a DM. Every mutation is CAS-guarded and account-scoped in
# the store; a stale or cross-account call returns a clear non-zero payload.

def _dm_page(config: AccountConfig):
    """Build the live browser page for DM sending (ThreadsDMPage)."""
    from . import dm_browser
    profile = config.get("THREADS_BROWSER_PROFILE", "") or "~/.threads-operator/browser-profiles/syaqir"
    return dm_browser.ThreadsDMPage(Path(profile).expanduser())


def _dm_expected_username(config: AccountConfig) -> str | None:
    """The account's real Threads username for browser-login verification.

    Read from optional config ``THREADS_USERNAME``. When unset, returns None so
    the sender falls back to the account_key label (legacy behaviour). Set this
    to the actual Threads handle (e.g. ``syaqir_sharani``) so account
    verification compares against the real login, not the operator label."""
    val = (config.get("THREADS_USERNAME", "") or "").strip().lstrip("@")
    return val or None


def _dm_result_payload(config: AccountConfig, res, *, code: int = 0) -> tuple[int, dict[str, Any]]:
    payload = {
        "ok": res.ok, "account": config.name,
        "id": res.opportunity_id, "status": "sent" if res.sent else None,
        "attempt": res.attempt, "confirmation_ref": res.confirmation_ref,
        "text_hash": res.text_hash, "target_username": res.target_username,
    }
    if not res.ok:
        payload["error"] = res.detail
        payload["failure_category"] = res.failure_category
        payload["uncertain"] = res.uncertain
        code = code or 1
    return (0 if res.ok else (code or 1)), payload


def _run_dm_send(
    config: AccountConfig, *, opportunity_id: int, worker_id: str | None
) -> tuple[int, dict[str, Any]]:
    """Claim + browser-send one approved DM opportunity. Fail-closed."""
    from . import dm_send
    store = _store(config)
    worker = worker_id or f"cli-{os.getpid()}"
    page = _dm_page(config)
    try:
        res = dm_send.send_dm_opportunity(
            store, opportunity_id, page=page, worker_id=worker,
            expected_account_username=_dm_expected_username(config))
    finally:
        page.close()
    return _dm_result_payload(config, res)


def _run_dm_send_next(
    config: AccountConfig, *, worker_id: str | None
) -> tuple[int, dict[str, Any]]:
    """Send the next approved opportunity (oldest first). No-op if none."""
    from . import dm_send
    store = _store(config)
    rows = store.list_dm_opportunities(status="approved", limit=1)
    if not rows:
        return 0, {"ok": True, "account": config.name, "sent": 0,
                   "note": "no approved opportunities"}
    oid = rows[0]["id"]
    return _run_dm_send(config, opportunity_id=oid, worker_id=worker_id)


def _run_dm_send_queue(config: AccountConfig, *, limit: int) -> tuple[int, dict[str, Any]]:
    """List the send-relevant opportunities (approved/sending/send_uncertain)."""
    store = _store(config)
    out = {"ok": True, "account": config.name}
    for st in ("approved", "sending", "send_uncertain"):
        rows = store.list_dm_opportunities(status=st, limit=limit)
        out[st] = [
            {"id": r.get("id"), "status": r.get("status"),
             "target": r.get("target_threads_username"),
             "attempt_count": r.get("attempt_count"),
             "claim_id": r.get("claim_id"),
             "failure_category": r.get("failure_category"),
             "sent_at": r.get("sent_at")}
            for r in rows
        ]
    return 0, out


def _run_dm_inspect(config: AccountConfig, *, opportunity_id: int) -> tuple[int, dict[str, Any]]:
    store = _store(config)
    row = store.get_dm_opportunity(opportunity_id)
    if row is None:
        return 2, {"ok": False, "account": config.name,
                   "error": f"DM opportunity {opportunity_id} not found (or not this account)"}
    keys = ("id", "status", "target_threads_username", "attempt_count", "claim_id",
            "claimed_at", "last_attempt_at", "sent_at", "external_dm_id",
            "confirmation_ref", "confirmation_evidence", "failure_category",
            "last_error", "sent_text_hash", "dm_approved_text")
    return 0, {"ok": True, "account": config.name,
               "opportunity": {k: row.get(k) for k in keys}}


def _run_dm_reconcile(
    config: AccountConfig, *, opportunity_id: int
) -> tuple[int, dict[str, Any]]:
    """Resolve a send_uncertain opportunity via the browser (no resend)."""
    from . import dm_send
    store = _store(config)
    page = _dm_page(config)
    try:
        res = dm_send.reconcile_dm_opportunity(store, opportunity_id, page=page)
    finally:
        page.close()
    return (0 if res.resolved else 1), {
        "ok": res.resolved, "account": config.name, "id": opportunity_id,
        "outcome": res.outcome, "can_retry": res.can_retry, "detail": res.detail,
    }


def _run_dm_draft(
    config: AccountConfig, *, opportunity_id: int
) -> tuple[int, dict[str, Any]]:
    """Generate the DM draft (Hermes LLM) and persist it, then move the row to
    awaiting_approval. Idempotent for a row still in detected/drafted."""
    from . import dm_opportunity, own_replies, trend_engagement

    store = _store(config)
    row = store.get_dm_opportunity(opportunity_id)
    if row is None:
        return 2, {"ok": False, "account": config.name,
                   "error": f"DM opportunity {opportunity_id} not found (or not this account)"}
    if dm_opportunity.is_expired(row):
        return 2, {"ok": False, "account": config.name, "id": opportunity_id,
                   "error": f"DM opportunity {opportunity_id} has expired"}
    status = row.get("status")
    if status == dm_opportunity.DM_STATUS_AWAITING_APPROVAL:
        # Watchdog retry after a card was already prepared: do not re-draft.
        return 0, {"ok": True, "account": config.name, "id": opportunity_id,
                   "status": status, "dm_draft_text": row.get("dm_draft_text"),
                   "already_drafted": True,
                   "card_text": _dm_card_text(row)}
    if status not in (dm_opportunity.DM_STATUS_DETECTED, dm_opportunity.DM_STATUS_DRAFTED):
        return 2, {"ok": False, "account": config.name, "id": opportunity_id,
                   "status": status, "already_decided": status in dm_opportunity.DM_TERMINAL_STATUSES,
                   "error": f"DM opportunity {opportunity_id} is {status}; cannot draft"}

    persona_text = trend_engagement.load_persona(_personas_root(), config.name)
    try:
        draft = own_replies.generate_dm_draft(
            store=store, opportunity=row, persona_text=persona_text
        )
    except Exception as exc:  # noqa: BLE001
        return 1, {"ok": False, "account": config.name, "id": opportunity_id,
                   "error": f"draft generation failed: {type(exc).__name__}: {exc}"}

    drafted = dm_opportunity.draft_dm_opportunity(
        store=store, opportunity_id=opportunity_id, draft_text=draft
    )
    if drafted is None:
        return 2, {"ok": False, "account": config.name, "id": opportunity_id,
                   "error": "failed to persist draft (state changed concurrently)"}
    moved = dm_opportunity.mark_dm_awaiting_approval(
        store=store, opportunity_id=opportunity_id, approval_ref=None
    )
    final = moved or drafted
    return 0, {"ok": True, "account": config.name, "id": opportunity_id,
               "status": final.get("status"), "dm_draft_text": draft,
               "card_text": _dm_card_text(final)}


def _run_dm_card(
    config: AccountConfig, *, opportunity_id: int
) -> tuple[int, dict[str, Any]]:
    """Render the approval card text for one opportunity (watchdog/dispatcher)."""
    store = _store(config)
    row = store.get_dm_opportunity(opportunity_id)
    if row is None:
        return 2, {"ok": False, "account": config.name,
                   "error": f"DM opportunity {opportunity_id} not found (or not this account)"}
    return 0, {"ok": True, "account": config.name, "id": opportunity_id,
               "status": row.get("status"), "card_text": _dm_card_text(row)}


def _dm_card_text(row: dict[str, Any]) -> str:
    """Local card renderer (kept here so the CLI is self-contained; the
    dispatcher may also render its own copy)."""
    from . import workflow_telegram
    return workflow_telegram.dm_approval_card_text(row)


def _run_dm_approve(
    config: AccountConfig, *, opportunity_id: int, approval_ref: str | None
) -> tuple[int, dict[str, Any]]:
    from . import dm_opportunity

    store = _store(config)
    row = store.get_dm_opportunity(opportunity_id)
    if row is None:
        return 2, {"ok": False, "account": config.name,
                   "error": f"DM opportunity {opportunity_id} not found (or not this account)"}
    if row.get("status") == dm_opportunity.DM_STATUS_APPROVED:
        # Duplicate Approve tap / retried callback: idempotent success.
        return 0, {"ok": True, "account": config.name, "id": opportunity_id,
                   "status": "approved", "already_decided": True}
    if dm_opportunity.is_expired(row):
        return 2, {"ok": False, "account": config.name, "id": opportunity_id,
                   "status": row.get("status"),
                   "error": f"DM opportunity {opportunity_id} has expired and cannot be approved"}
    approved_by = None
    if approval_ref:
        approved_by = approval_ref  # "tg:<chat>:<msg>" — identifies the approver channel
    moved = dm_opportunity.approve_dm_opportunity(
        store=store, opportunity_id=opportunity_id, approved_by=approved_by
    )
    if moved is None:
        current = store.get_dm_opportunity(opportunity_id) or {}
        return 2, {"ok": False, "account": config.name, "id": opportunity_id,
                   "status": current.get("status"),
                   "already_decided": current.get("status") in dm_opportunity.DM_TERMINAL_STATUSES
                       or current.get("status") == dm_opportunity.DM_STATUS_APPROVED,
                   "error": f"DM opportunity {opportunity_id} is not awaiting approval"}
    return 0, {"ok": True, "account": config.name, "id": opportunity_id,
               "status": moved.get("status"), "dm_approved_text": moved.get("dm_approved_text")}


def _run_dm_reject(
    config: AccountConfig, *, opportunity_id: int, approval_ref: str | None
) -> tuple[int, dict[str, Any]]:
    from . import dm_opportunity

    store = _store(config)
    row = store.get_dm_opportunity(opportunity_id)
    if row is None:
        return 2, {"ok": False, "account": config.name,
                   "error": f"DM opportunity {opportunity_id} not found (or not this account)"}
    if row.get("status") in dm_opportunity.DM_TERMINAL_STATUSES:
        return 2, {"ok": False, "account": config.name, "id": opportunity_id,
                   "status": row.get("status"), "already_decided": True,
                   "error": f"DM opportunity {opportunity_id} is already {row.get('status')}"}
    moved = dm_opportunity.reject_dm_opportunity(
        store=store, opportunity_id=opportunity_id
    )
    if moved is None:
        return 2, {"ok": False, "account": config.name, "id": opportunity_id,
                   "error": f"failed to reject DM opportunity {opportunity_id}"}
    return 0, {"ok": True, "account": config.name, "id": opportunity_id,
               "status": moved.get("status")}


def _run_dm_edit(
    config: AccountConfig, *, opportunity_id: int, text: str
) -> tuple[int, dict[str, Any]]:
    from . import dm_opportunity, own_replies

    if not text or not text.strip():
        return 2, {"ok": False, "account": config.name, "id": opportunity_id,
                   "error": "edited DM text is empty"}
    if len(text) > own_replies.DM_CHAR_LIMIT:
        return 2, {"ok": False, "account": config.name, "id": opportunity_id,
                   "error": f"edited DM is {len(text)} chars (limit {own_replies.DM_CHAR_LIMIT})"}
    store = _store(config)
    row = store.get_dm_opportunity(opportunity_id)
    if row is None:
        return 2, {"ok": False, "account": config.name,
                   "error": f"DM opportunity {opportunity_id} not found (or not this account)"}
    if dm_opportunity.is_expired(row):
        return 2, {"ok": False, "account": config.name, "id": opportunity_id,
                   "status": row.get("status"),
                   "error": f"DM opportunity {opportunity_id} has expired and cannot be edited"}
    try:
        moved = dm_opportunity.edit_dm_opportunity_draft(
            store=store, opportunity_id=opportunity_id, edited_text=text.strip()
        )
    except ValueError as exc:
        return 2, {"ok": False, "account": config.name, "id": opportunity_id,
                   "error": str(exc)}
    if moved is None:
        current = store.get_dm_opportunity(opportunity_id) or {}
        return 2, {"ok": False, "account": config.name, "id": opportunity_id,
                   "status": current.get("status"),
                   "already_decided": current.get("status") in dm_opportunity.DM_TERMINAL_STATUSES,
                   "error": f"DM opportunity {opportunity_id} is not awaiting approval"}
    return 0, {"ok": True, "account": config.name, "id": opportunity_id,
               "status": moved.get("status"),
               "dm_draft_text": moved.get("dm_draft_text"),
               "card_text": _dm_card_text(moved)}


def _run_dm_mark_card_sent(
    config: AccountConfig, *, opportunity_id: int, message_ref: str
) -> tuple[int, dict[str, Any]]:
    from . import dm_opportunity

    store = _store(config)
    moved = dm_opportunity.record_dm_approval_card_sent(
        store=store, opportunity_id=opportunity_id, message_ref=message_ref
    )
    if moved is None:
        return 2, {"ok": False, "account": config.name, "id": opportunity_id,
                   "error": f"DM opportunity {opportunity_id} is not awaiting approval (or not this account)"}
    return 0, {"ok": True, "account": config.name, "id": opportunity_id,
               "approval_message_ref": moved.get("approval_message_ref")}


def _run_dm_needing_card(
    config: AccountConfig, *, limit: int
) -> tuple[int, dict[str, Any]]:
    from . import dm_opportunity

    store = _store(config)
    rows = dm_opportunity.list_dm_opportunities_needing_card(store, limit=limit)
    return 0, {"ok": True, "account": config.name, "count": len(rows),
               "items": [{"id": r.get("id"), "status": r.get("status"),
                          "from_username": r.get("from_username"),
                          "has_draft": bool(r.get("dm_draft_text")),
                          "has_card": bool(r.get("approval_message_ref"))}
                         for r in rows]}


def _run_ownreply_publish(
    config: AccountConfig, *, row_id: int | None, dry_run: bool
) -> tuple[int, dict[str, Any]]:
    """Publish approved replies via the official Graph API. Claim-first CAS
    prevents double-publishes; transient errors return the row to approved,
    permanent errors burn it to failed with the exact API error recorded."""
    from . import own_replies

    store = _store(config)
    if row_id is not None:
        row = store.get_own_reply(row_id)
        targets = [row] if row and row.get("status") == "approved" else []
    else:
        targets = store.list_own_replies(status="approved", limit=50)

    if dry_run:
        return 0, {"ok": True, "account": config.name, "dry_run": True,
                   "eligible": len(targets),
                   "ids": [t.get("id") for t in targets]}
    if not config.get_bool("THREADS_ENGAGEMENT_ENABLED"):
        return 3, {"ok": False, "account": config.name,
                   "error": "Live engagement is disabled; require THREADS_ENGAGEMENT_ENABLED=true"}

    api = _api(config)
    results: list[dict[str, Any]] = []
    for target in targets:
        tid = int(target["id"])
        claimed = store.transition_own_reply(
            row_id=tid, from_status="approved", to_status="posting",
            fields={"claimed_at": store._utc_now(),
                    "attempt_count": int(target.get("attempt_count") or 0) + 1},
        )
        if claimed is None:
            results.append({"id": tid, "status": "skipped",
                            "reason": "already claimed by another worker"})
            continue
        try:
            published_id = api.publish_text(
                str(claimed.get("proposed_text") or ""),
                reply_to_id=str(claimed.get("reply_id") or ""),
            )
            posted = store.transition_own_reply(
                row_id=tid, from_status="posting", to_status="posted",
                fields={"external_action_id": published_id,
                        "executed_at": store._utc_now()},
            )
            results.append({"id": tid, "status": "posted",
                            "published_reply_id": published_id,
                            "row": posted})
        except Exception as exc:  # noqa: BLE001
            error = redact_error(f"{type(exc).__name__}: {exc}", _known_secrets(config))
            attempts = int(claimed.get("attempt_count") or 1)
            if own_replies.is_transient_error(error) and attempts < own_replies.MAX_REPLY_ATTEMPTS:
                store.transition_own_reply(
                    row_id=tid, from_status="posting", to_status="approved",
                    fields={"last_error": error},
                )
                results.append({"id": tid, "status": "approved",
                                "retry_scheduled": True, "error": error})
            else:
                store.transition_own_reply(
                    row_id=tid, from_status="posting", to_status="failed",
                    fields={"last_error": error, "executed_at": store._utc_now()},
                )
                results.append({"id": tid, "status": "failed", "error": error,
                                "permanent": not own_replies.is_transient_error(error)})

    posted_n = sum(1 for r in results if r.get("status") == "posted")
    failed_n = sum(1 for r in results if r.get("status") == "failed")
    return (0 if failed_n == 0 else 1), {
        "ok": failed_n == 0,
        "account": config.name,
        "processed": len(results),
        "posted": posted_n,
        "failed": failed_n,
        "results": results,
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
                topic=args.topic,
            )
        elif args.command == "publish":
            code, payload = _run_publish(config, dry_run=args.dry_run)
        elif args.command == "publish-worker":
            code, payload = _run_publish_worker(
                config,
                campaign_code=args.campaign_code,
                dry_run=args.dry_run,
            )
        elif args.command == "engagement" and args.engagement_command == "propose-reply":
            code, payload = _run_engagement_propose_reply(
                config,
                source_post_id=args.source_post_id,
                url=args.url,
                text=args.text,
                username=args.username,
                source_text=args.source_text,
                candidate_id=args.candidate_id,
                score=args.score,
                reason=args.reason,
                expires_at=args.expires_at,
            )
        elif args.command == "engagement" and args.engagement_command == "list":
            code, payload = _run_engagement_list(
                config, status=args.status, limit=args.limit
            )
        elif args.command == "engagement" and args.engagement_command == "approve":
            code, payload = _run_engagement_approve(
                config, engagement_id=args.id, approval_ref=args.approval_ref
            )
        elif args.command == "engagement" and args.engagement_command == "reject":
            code, payload = _run_engagement_reject(
                config, engagement_id=args.id, approval_ref=args.approval_ref
            )
        elif args.command == "engagement" and args.engagement_command == "edit":
            code, payload = _run_engagement_edit(
                config, engagement_id=args.id, text=args.text
            )
        elif args.command == "engagement" and args.engagement_command == "execute":
            code, payload = _run_engagement_execute(
                config, engagement_id=args.id, dry_run=args.dry_run
            )
        elif args.command == "trend" and args.trend_command == "add":
            code, payload = _run_trend_add(
                config,
                url=args.url,
                text=args.text,
                username=args.username,
                external=args.external,
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
        elif args.command == "trend-engagement" and args.trendeng_command == "draft":
            code, payload = _run_trendeng_draft(
                config,
                candidate_id=args.id,
                limit=args.limit,
                threshold=args.threshold,
                dry_run=args.dry_run,
            )
        elif args.command == "trend-engagement" and args.trendeng_command == "list":
            code, payload = _run_trendeng_list(
                config, status=args.status, limit=args.limit
            )
        elif args.command == "trend-engagement" and args.trendeng_command == "approve":
            code, payload = _run_trendeng_approve(config, candidate_id=args.id)
        elif args.command == "trend-engagement" and args.trendeng_command == "schedule-approve":
            code, payload = _run_trendeng_schedule_approve(config, candidate_id=args.id)
        elif args.command == "trend-engagement" and args.trendeng_command == "schedule-best":
            code, payload = _run_trendeng_schedule_best(config, candidate_id=args.id)
        elif args.command == "trend-engagement" and args.trendeng_command == "schedule-now":
            code, payload = _run_trendeng_schedule_now(config, candidate_id=args.id)
        elif args.command == "trend-engagement" and args.trendeng_command == "schedule-time":
            code, payload = _run_trendeng_schedule_time(
                config, candidate_id=args.id, at=args.at
            )
        elif args.command == "trend-engagement" and args.trendeng_command == "schedule-cancel":
            code, payload = _run_trendeng_schedule_cancel(config, candidate_id=args.id)
        elif args.command == "trend-engagement" and args.trendeng_command == "edit":
            code, payload = _run_trendeng_edit(
                config, candidate_id=args.id, text=args.text
            )
        elif args.command == "trend-engagement" and args.trendeng_command == "reject":
            code, payload = _trendeng_set_status(
                config, candidate_id=args.id,
                from_status="pending_approval", to_status="rejected",
            )
        elif args.command == "trend-engagement" and args.trendeng_command == "skip":
            code, payload = _run_trendeng_skip(config, candidate_id=args.id)
        elif args.command == "trend-engagement" and args.trendeng_command == "backlog":
            code, payload = _run_trendeng_backlog(config, limit=args.limit)
        elif args.command == "trend-engagement" and args.trendeng_command == "timeout":
            code, payload = _run_trendeng_timeout(
                config, older_than_hours=args.older_than_hours, dry_run=args.dry_run
            )
        elif args.command == "trend-engagement" and args.trendeng_command == "telegram-send":
            code, payload = _run_trendeng_telegram_send(
                config, candidate_id=args.id, card=args.card
            )
        elif args.command == "trend-engagement" and args.trendeng_command == "use":
            code, payload = _run_trendeng_use(config, candidate_id=args.id)
        elif args.command == "trend-engagement" and args.trendeng_command == "edit":
            code, payload = _run_trendeng_edit(config, candidate_id=args.id, text=args.text)
        elif args.command == "trend-engagement" and args.trendeng_command == "refresh":
            code, payload = _run_trendeng_refresh(config, candidate_id=args.id)
        elif args.command == "trend-engagement" and args.trendeng_command == "discard":
            code, payload = _run_trendeng_discard(config, candidate_id=args.id)
        elif args.command == "own-replies" and args.ownreply_command == "scan":
            code, payload = _run_ownreply_scan(
                config, limit=args.limit, propose=args.propose, dry_run=args.dry_run
            )
        elif args.command == "own-replies" and args.ownreply_command == "list":
            code, payload = _run_ownreply_list(
                config, status=args.status, limit=args.limit
            )
        elif args.command == "own-replies" and args.ownreply_command == "approve":
            code, payload = _run_ownreply_decision(
                config, row_id=args.id, decision="approve"
            )
        elif args.command == "own-replies" and args.ownreply_command == "edit":
            code, payload = _run_ownreply_edit(
                config, row_id=args.id, text=args.text
            )
        elif args.command == "own-replies" and args.ownreply_command == "reject":
            code, payload = _run_ownreply_decision(
                config, row_id=args.id, decision="reject"
            )
        elif args.command == "own-replies" and args.ownreply_command == "ignore":
            code, payload = _run_ownreply_decision(
                config, row_id=args.id, decision="ignore"
            )
        elif args.command == "own-replies" and args.ownreply_command == "publish-approved":
            code, payload = _run_ownreply_publish(
                config, row_id=args.id, dry_run=args.dry_run
            )
        elif args.command == "own-replies" and args.ownreply_command == "dm" and args.dm_command == "draft":
            code, payload = _run_dm_draft(config, opportunity_id=args.id)
        elif args.command == "own-replies" and args.ownreply_command == "dm" and args.dm_command == "card":
            code, payload = _run_dm_card(config, opportunity_id=args.id)
        elif args.command == "own-replies" and args.ownreply_command == "dm" and args.dm_command == "approve":
            code, payload = _run_dm_approve(
                config, opportunity_id=args.id, approval_ref=args.approval_ref
            )
        elif args.command == "own-replies" and args.ownreply_command == "dm" and args.dm_command == "edit":
            code, payload = _run_dm_edit(config, opportunity_id=args.id, text=args.text)
        elif args.command == "own-replies" and args.ownreply_command == "dm" and args.dm_command == "reject":
            code, payload = _run_dm_reject(
                config, opportunity_id=args.id, approval_ref=args.approval_ref
            )
        elif args.command == "own-replies" and args.ownreply_command == "dm" and args.dm_command == "mark-card-sent":
            code, payload = _run_dm_mark_card_sent(
                config, opportunity_id=args.id, message_ref=args.message_ref
            )
        elif args.command == "own-replies" and args.ownreply_command == "dm" and args.dm_command == "needing-card":
            code, payload = _run_dm_needing_card(config, limit=args.limit)
        elif args.command == "own-replies" and args.ownreply_command == "dm" and args.dm_command == "send":
            code, payload = _run_dm_send(config, opportunity_id=args.id, worker_id=args.worker_id)
        elif args.command == "own-replies" and args.ownreply_command == "dm" and args.dm_command == "send-next":
            code, payload = _run_dm_send_next(config, worker_id=args.worker_id)
        elif args.command == "own-replies" and args.ownreply_command == "dm" and args.dm_command == "send-queue":
            code, payload = _run_dm_send_queue(config, limit=args.limit)
        elif args.command == "own-replies" and args.ownreply_command == "dm" and args.dm_command == "inspect":
            code, payload = _run_dm_inspect(config, opportunity_id=args.id)
        elif args.command == "own-replies" and args.ownreply_command == "dm" and args.dm_command == "reconcile":
            code, payload = _run_dm_reconcile(config, opportunity_id=args.id)
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
