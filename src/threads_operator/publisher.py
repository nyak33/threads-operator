"""Deterministic publisher for one account-scoped Supabase queue."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any

from .supabase_store import SupabaseStore
from .threads_api import ThreadsAPI

RECOVERY_WINDOW = timedelta(minutes=30)
_RETRY_DELAYS_SECONDS = (15, 30, 60, 120, 300)


def _reply_texts(row: dict[str, Any]) -> list[str]:
    raw = row.get("reply_texts")
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("reply_texts must be a JSON array") from exc
    if not isinstance(raw, list):
        raise ValueError("reply_texts must be an array")
    replies: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("reply_texts entries must be strings")
        text = item.strip()
        if text:
            replies.append(item)
    return replies


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _retry_delay(attempt_count: int) -> int:
    index = max(0, min(attempt_count - 1, len(_RETRY_DELAYS_SECONDS) - 1))
    return _RETRY_DELAYS_SECONDS[index]


def _error_details(exc: Exception) -> tuple[str, dict[str, Any], bool | None]:
    error = f"{type(exc).__name__}: {exc}"
    diagnostics = getattr(exc, "diagnostics", {})
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    retryable = getattr(exc, "retryable", None)
    if not isinstance(retryable, bool):
        retryable = None
    return error, dict(diagnostics), retryable


def _recovery_deadline(row: dict[str, Any]) -> datetime | None:
    existing = _parse_time(row.get("retry_deadline_at"))
    if existing is not None:
        return existing
    scheduled = _parse_time(row.get("scheduled_at"))
    if scheduled is None:
        return None
    return scheduled + RECOVERY_WINDOW


def publish_next(
    api: ThreadsAPI,
    store: SupabaseStore,
    table: str,
    campaign_code: str | None = None,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Publish the oldest due approved/retrying queue row after winning its claim.

    Queue-level recovery owns temporary publish failures. A retry creates a fresh
    Threads container because every scheduler invocation calls ``publish_text``
    from scratch. Partial publishes remain terminal/manual to avoid duplicating a
    main post whose ID has already been persisted.
    """
    current = (now or _utc_now()).astimezone(timezone.utc)
    current_iso = _iso(current)

    if dry_run:
        candidate = store.peek_due_post(
            table,
            campaign_code=campaign_code,
            now=current_iso,
        )
        if not candidate:
            return {"status": "idle"}
        return {"status": "dry-run", "queue_id": candidate.get("id")}

    row = store.claim_due_post(
        table,
        campaign_code=campaign_code,
        now=current_iso,
    )
    if not row:
        return {"status": "idle"}

    row_id = row.get("id")
    if row_id is None:
        raise ValueError("Claimed queue row is missing id")

    deadline = _recovery_deadline(row)
    if deadline is not None and current >= deadline:
        error = "Automatic publish recovery window expired before publish attempt"
        diagnostics = {
            "classification": "recovery_window_expired",
            "scheduled_at": row.get("scheduled_at"),
            "retry_deadline_at": _iso(deadline),
        }
        store.mark_post_needs_attention(table, row_id, error, diagnostics)
        return {
            "status": "needs_attention",
            "queue_id": row_id,
            "error": error,
            "diagnostics": diagnostics,
        }

    main_post_id: str | None = None
    reply_ids: list[str] = []
    try:
        main_text = row.get("main_post_text")
        if not isinstance(main_text, str) or not main_text.strip():
            raise ValueError("Claimed queue row has no main_post_text")
        replies = _reply_texts(row)

        main_post_id = api.publish_text(main_text)
        store.mark_post_main_published(table, row_id, main_post_id)

        parent_id = main_post_id
        for reply_text in replies:
            reply_id = api.publish_text(reply_text, reply_to_id=parent_id)
            reply_ids.append(reply_id)
            store.mark_post_reply_progress(table, row_id, reply_ids)
            parent_id = reply_id

        store.mark_post_posted(table, row_id, reply_ids)
        return {
            "status": "posted",
            "queue_id": row_id,
            "main_post_id": main_post_id,
            "reply_ids": reply_ids,
        }
    except Exception as exc:
        error, diagnostics, retryable = _error_details(exc)

        # Once the main post exists, blind queue-level retry could duplicate it.
        # Preserve the existing conservative manual-reconciliation behavior.
        if main_post_id is not None:
            try:
                store.mark_post_failed(table, row_id, error)
            except Exception as persistence_exc:
                error = (
                    f"{error}; failed to persist failure state: "
                    f"{type(persistence_exc).__name__}: {persistence_exc}"
                )
            result: dict[str, Any] = {
                "status": "failed",
                "queue_id": row_id,
                "error": error,
                "main_post_id": main_post_id,
            }
            if reply_ids:
                result["reply_ids"] = reply_ids
            return result

        if retryable is True:
            effective_deadline = deadline or (current + RECOVERY_WINDOW)
            attempt_count = int(row.get("attempt_count") or 0) + 1

            if current < effective_deadline:
                next_retry = min(
                    current + timedelta(seconds=_retry_delay(attempt_count)),
                    effective_deadline,
                )
                try:
                    store.mark_post_retrying(
                        table,
                        row_id,
                        error,
                        diagnostics,
                        _iso(next_retry),
                        _iso(effective_deadline),
                        attempt_count,
                    )
                except Exception as persistence_exc:
                    error = (
                        f"{error}; failed to persist retry state: "
                        f"{type(persistence_exc).__name__}: {persistence_exc}"
                    )
                    return {"status": "failed", "queue_id": row_id, "error": error}
                return {
                    "status": "retrying",
                    "queue_id": row_id,
                    "error": error,
                    "attempt_count": attempt_count,
                    "next_retry_at": _iso(next_retry),
                    "retry_deadline_at": _iso(effective_deadline),
                    "diagnostics": diagnostics,
                }

            try:
                store.mark_post_needs_attention(table, row_id, error, diagnostics)
            except Exception as persistence_exc:
                error = (
                    f"{error}; failed to persist needs-attention state: "
                    f"{type(persistence_exc).__name__}: {persistence_exc}"
                )
            return {
                "status": "needs_attention",
                "queue_id": row_id,
                "error": error,
                "diagnostics": diagnostics,
            }

        if retryable is False:
            try:
                store.mark_post_needs_attention(table, row_id, error, diagnostics)
            except Exception as persistence_exc:
                error = (
                    f"{error}; failed to persist needs-attention state: "
                    f"{type(persistence_exc).__name__}: {persistence_exc}"
                )
            return {
                "status": "needs_attention",
                "queue_id": row_id,
                "error": error,
                "diagnostics": diagnostics,
            }

        # Non-Threads/internal errors keep the legacy terminal failure semantics.
        try:
            store.mark_post_failed(table, row_id, error)
        except Exception as persistence_exc:
            error = (
                f"{error}; failed to persist failure state: "
                f"{type(persistence_exc).__name__}: {persistence_exc}"
            )
        return {
            "status": "failed",
            "queue_id": row_id,
            "error": error,
        }
