"""Account-scoped publish worker with automatic transient retry.

Wraps :func:`threads_operator.publisher.publish_next` with the recovery loop
the production cron flow needs: when a publish fails with a *transient* error
(network, 429, 5xx, or a non-OAuth 400 — the same classification the API
adapter uses for its own retries), the queue row is reset to ``approved`` so
the next scheduled tick picks it up automatically. Genuinely non-recoverable
errors (OAuth/auth, permission, permanent rejection) stay ``failed`` for
manual review.

This is the deterministic, no-LLM worker intended to run on a short cron
interval (for example every 5 minutes). Selection is account-scoped and
oldest-first; an optional campaign code narrows it further.
"""

from __future__ import annotations

from typing import Any

from .publisher import publish_next
from .supabase_store import SupabaseStore
from .threads_api import (
    ThreadsAPI,
    _is_non_retryable_publish_error,
    _is_transient_publish_error,
)


def is_transient_publish_result(result: dict[str, Any]) -> bool:
    """Classify a failed publish_next result as transient (retryable).

    Works from the error string the publisher produces, mirroring the API
    adapter's own transient/non-retryable semantics. OAuth and permission
    errors are never transient; network, timeout, 429, 5xx, and non-OAuth 400
    are transient.
    """
    error = str(result.get("error", "")).lower()
    if not error:
        return False
    # Non-recoverable markers first.
    if "oauth" in error or "permission" in error:
        return False
    if "invalid" in error and "token" in error:
        return False
    # Transient markers.
    if "429" in error or "5" == error.strip()[:1]:  # 5xx status code text
        return True
    if "networkerror" in error or "timeoutexception" in error or "connecterror" in error:
        return True
    if "400" in error or "bad request" in error:
        return True
    if "httpstatuserror" in error:
        # Unclassifiable HTTP error — treat as transient (Meta publish is flaky).
        return True
    # Default: conservative, do not auto-requeue unknown failures.
    return False


def requeue_failed_row(
    store: SupabaseStore,
    table: str,
    row_id: int | str,
    error: str,
) -> bool:
    """Reset a failed/posting row back to ``approved`` for the next tick.

    Guards on current status so a row already advanced by another worker is
    never regressed. Returns True when the row was actually requeued.
    """
    account_key = store._require_account_key()
    headers = {**store._headers, "Prefer": "return=representation"}
    response = store.client.patch(
        store._queue_url(table),
        headers=headers,
        params={
            "id": f"eq.{row_id}",
            "account_key": f"eq.{account_key}",
            "status": "in.(failed,posting)",
        },
        json={"status": "approved", "last_error": str(error)[:2000]},
    )
    response.raise_for_status()
    rows = response.json() or []
    return bool(rows)


def publish_next_with_recovery(
    api: ThreadsAPI,
    store: SupabaseStore,
    table: str,
    campaign_code: str | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Publish the oldest due approved row, auto-requeuing transient failures.

    Returns the publish_next result, augmented with ``requeued=True`` when a
    transient failure was reset to ``approved`` for the next scheduled run.
    """
    result = publish_next(
        api, store, table, campaign_code=campaign_code, dry_run=dry_run
    )
    if dry_run or result.get("status") != "failed":
        return result

    row_id = result.get("queue_id")
    error = result.get("error", "unknown error")
    if row_id is None:
        return result
    if is_transient_publish_result(result):
        try:
            if requeue_failed_row(store, table, row_id, error):
                result["requeued"] = True
        except Exception:
            # A failed requeue must not mask the original publish failure.
            result["requeued"] = False
    return result
