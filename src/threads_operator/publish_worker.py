"""Account-scoped publish worker with automatic transient retry.

Wraps :func:`threads_operator.publisher.publish_next` with the recovery loop
the production cron flow needs. Meta intermittently answers ``threads_publish``
with a bare 400 during bad windows that can outlast a single short retry burst,
so the worker persists *within the run*: each transient failure is requeued to
``approved`` and the same run re-claims and retries with a fresh container and
backoff, until either the publish succeeds, a non-transient error stops it, or
the per-run deadline is hit (leaving the row ``approved`` for the next tick).
Genuinely non-recoverable errors (OAuth/auth, permission) stay ``failed`` for
manual review.

This is the deterministic, no-LLM worker intended to run on a short cron
interval (for example every 5 minutes). Selection is account-scoped and
oldest-first; an optional campaign code narrows it further. Only
``status=approved`` rows with ``scheduled_at <= now`` are ever published, so a
future-scheduled row is never posted early regardless of retries.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .publisher import publish_next
from .supabase_store import SupabaseStore
from .threads_api import ThreadsAPI

# Defaults tuned for a */5 cron tick: keep fighting a transient window within
# the same run, but always yield well before the next tick so runs never pile
# up. A hard wall-clock cap also bounds pathological cases.
DEFAULT_ATTEMPT_DEADLINE_SECONDS = 120.0
DEFAULT_MAX_ATTEMPTS = 8
ATTEMPT_BACKOFF_SECONDS = (2.0, 5.0, 10.0, 15.0, 20.0, 30.0, 30.0, 30.0)


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


def is_rate_limited_publish_result(result: dict[str, Any]) -> bool:
    """Return True when the failure should wait for the next cron tick."""
    error = str(result.get("error", "")).lower()
    if not error:
        return False
    if "429" in error or "too many requests" in error or "rate limit" in error:
        return True
    if "rate-limit" in error or "throttl" in error:
        return True
    if re.search(r"\bcode=(4|17|32|613)\b", error):
        return True
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


def _backoff_seconds(attempt_index: int) -> float:
    if attempt_index < len(ATTEMPT_BACKOFF_SECONDS):
        return ATTEMPT_BACKOFF_SECONDS[attempt_index]
    return ATTEMPT_BACKOFF_SECONDS[-1]


def publish_next_with_recovery(
    api: ThreadsAPI,
    store: SupabaseStore,
    table: str,
    campaign_code: str | None = None,
    *,
    dry_run: bool = False,
    attempt_deadline_seconds: float = DEFAULT_ATTEMPT_DEADLINE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sleep: Any = time.sleep,
    clock: Any = time.monotonic,
) -> dict[str, Any]:
    """Publish the oldest due approved row, persisting through transient failure.

    Each transient failure requeues the row to ``approved`` and, while the
    per-run deadline and attempt budget allow, the same run re-claims and
    retries with a fresh container after a growing backoff. The loop only ever
    re-selects ``approved`` rows with ``scheduled_at <= now`` (oldest first),
    so scheduled future posts are never posted early.

    Returns the final publish_next result, augmented with ``requeued=True``
    when the last failure was reset to ``approved`` for the next scheduled run,
    and ``attempts`` recording how many publish passes this run made.
    """
    if dry_run:
        return publish_next(api, store, table, campaign_code=campaign_code, dry_run=True)

    deadline = clock() + attempt_deadline_seconds
    attempts = 0
    while True:
        result = publish_next(api, store, table, campaign_code=campaign_code)
        if result.get("status") != "failed":
            if attempts:
                result["attempts"] = attempts + 1
            return result

        row_id = result.get("queue_id")
        error = result.get("error", "unknown error")
        if row_id is None:
            result["attempts"] = attempts + 1
            return result

        if is_transient_publish_result(result):
            try:
                if requeue_failed_row(store, table, row_id, error):
                    result["requeued"] = True
            except Exception:
                # A failed requeue must not mask the original publish failure.
                result["requeued"] = False

        attempts += 1
        result["attempts"] = attempts
        if result.get("requeued") and is_rate_limited_publish_result(result):
            # Persist the approved state, then yield. Immediate retries make
            # throttling worse and contradict the intended cron backoff.
            return result
        if not result.get("requeued") or attempts >= max_attempts:
            return result
        wait = _backoff_seconds(attempts - 1)
        if clock() + wait >= deadline:
            # Not enough budget left to retry within this run — leave the row
            # approved for the next scheduled tick.
            return result
        sleep(wait)
