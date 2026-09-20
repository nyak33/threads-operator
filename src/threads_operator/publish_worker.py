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
from datetime import datetime, timedelta, timezone
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

# Durable retry policy (persisted on the row so it survives across separate
# executor processes). A transiently-failing row is retried up to
# DEFAULT_MAX_PERSISTED_ATTEMPTS times with a growing persisted backoff
# (next_retry_at), then marked failed and alerted. Persisted backoff means the
# same poisoned row is never re-selected every cron tick.
DEFAULT_MAX_PERSISTED_ATTEMPTS = 5
# Persisted backoff schedule (seconds) indexed by attempt number. After the
# last entry the backoff stays at the final value.
PERSISTED_BACKOFF_SECONDS = (300, 900, 1800, 3600, 7200)  # 5m,15m,30m,1h,2h

# Bounded backlog processing: how many eligible due rows one executor run may
# publish, in oldest-first order, to recover a backlog without one run
# monopolising. Rows are still published sequentially (never concurrently for
# the same account), preserving claim safety and recovery guarantees.
DEFAULT_MAX_ROWS_PER_RUN = 3

# Structured Meta errors that must never be auto-requeued: (code, subcode).
# code 24 / subcode 4279009 is the observed "resource does not exist" content
# rejection — permanent as-coded, manual review only.
NON_RETRYABLE_META_CODES = frozenset({(24, 4279009)})
# Meta rejects over-length text with a 500 whose message carries this marker.
_META_TEXT_LIMIT = 500

_META_CODE_RE = re.compile(r'"code"\s*:\s*(\d+)')
_META_SUBCODE_RE = re.compile(r'"error_subcode"\s*:\s*(\d+)')
_META_MESSAGE_RE = re.compile(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"')
_META_TYPE_RE = re.compile(r'"type"\s*:\s*"([^"]+)"')
_HTTP_STATUS_RE = re.compile(r"\b([1-5]\d\d)\b")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_plus(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def extract_meta_error(result: dict[str, Any]) -> dict[str, Any]:
    """Pull structured Meta error fields out of a failed publish_next result.

    Returns a dict with keys: http_status (int|None), code (int|None),
    subcode (int|None), type (str|None), message (str|None), raw (str).
    Falls back to parsing the error string the publisher/API adapter produces.
    """
    error = str(result.get("error", "") or "")
    parsed: dict[str, Any] = {
        "http_status": None,
        "code": None,
        "subcode": None,
        "type": None,
        "message": None,
        "raw": error,
    }
    code = _META_CODE_RE.search(error)
    if code:
        parsed["code"] = int(code.group(1))
    subcode = _META_SUBCODE_RE.search(error)
    if subcode:
        parsed["subcode"] = int(subcode.group(1))
    mtype = _META_TYPE_RE.search(error)
    if mtype:
        parsed["type"] = mtype.group(1)
    msg = _META_MESSAGE_RE.search(error)
    if msg:
        parsed["message"] = msg.group(1).encode().decode("unicode_escape", "ignore")
    status = _HTTP_STATUS_RE.search(error)
    if status:
        parsed["http_status"] = int(status.group(1))
    return parsed


def is_permanent_content_rejection(result: dict[str, Any]) -> bool:
    """True when Meta rejected the *content* such that retrying can never succeed.

    Covers the two observed permanent cases without relying on broad string
    heuristics alone:
      * structured code 24 / subcode 4279009 (resource does not exist), and
      * over-length text (> 500 chars) which Meta reports as a 500 whose message
        states the text limit — retrying the identical text always 500s.
    """
    meta = extract_meta_error(result)
    if (meta["code"], meta["subcode"]) in NON_RETRYABLE_META_CODES:
        return True
    message = (meta.get("message") or meta["raw"]).lower()
    if "500" in message or meta.get("http_status") == 500 or meta["http_status"] is None:
        if f"{_META_TEXT_LIMIT}" in message and (
            "char" in message or "too long" in message or "exceed" in message or "limit" in message
        ):
            return True
    return False


def is_manual_review_result(result: dict[str, Any]) -> bool:
    """True when the failure must stop auto-retry and await a human decision."""
    return is_permanent_content_rejection(result)


def is_transient_publish_result(result: dict[str, Any]) -> bool:
    """Classify a failed publish_next result as transient (retryable).

    Structured Meta error data takes precedence over string heuristics: a known
    permanent content rejection (code 24/subcode 4279009, or over-length text)
    is never transient. Otherwise OAuth and permission errors are never
    transient; network, timeout, 429, 5xx, and non-OAuth 400 are transient.
    """
    error = str(result.get("error", "")).lower()
    if not error:
        return False
    # Structured permanent content rejection wins over every heuristic below.
    if is_permanent_content_rejection(result):
        return False
    # Rate limits may be reported by Meta as OAuthException; they are still
    # recoverable, but must yield to the next cron tick.
    if is_rate_limited_publish_result(result):
        return True
    # Non-recoverable markers after the rate-limit exception above.
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
    *,
    next_retry_at: str | None = None,
    attempt_count: int | None = None,
    attempted_at: str | None = None,
) -> bool:
    """Reset a failed/posting row back to ``approved`` for the next tick.

    Guards on current status so a row already advanced by another worker is
    never regressed. When the table carries retry-state columns, also persists
    the durable attempt counter and the backoff gate (``next_retry_at``) so the
    row is not re-selected until the backoff elapses. Returns True when the row
    was actually requeued.
    """
    account_key = store._require_account_key()
    headers = {**store._headers, "Prefer": "return=representation"}
    payload: dict[str, Any] = {"status": "approved", "last_error": str(error)[:2000]}
    if next_retry_at is not None or attempt_count is not None or attempted_at is not None:
        from .supabase_store import _supports_retry_state

        if _supports_retry_state(table):
            if attempt_count is not None:
                payload["attempt_count"] = int(attempt_count)
            if attempted_at is not None:
                payload["last_attempt_at"] = attempted_at
            payload["next_retry_at"] = next_retry_at
    response = store.client.patch(
        store._queue_url(table),
        headers=headers,
        params={
            "id": f"eq.{row_id}",
            "account_key": f"eq.{account_key}",
            "status": "in.(failed,posting)",
        },
        json=payload,
    )
    response.raise_for_status()
    rows = response.json() or []
    return bool(rows)


def _persisted_backoff_seconds(prior_attempts: int) -> float:
    idx = max(0, min(prior_attempts - 1, len(PERSISTED_BACKOFF_SECONDS) - 1))
    return PERSISTED_BACKOFF_SECONDS[idx]


def _format_last_error(result: dict[str, Any], error: str) -> str:
    """Build a last_error string that preserves structured Meta fields."""
    meta = extract_meta_error(result)
    parts = []
    if meta.get("http_status"):
        parts.append(f"http={meta['http_status']}")
    if meta.get("code") is not None:
        parts.append(f"code={meta['code']}")
    if meta.get("subcode") is not None:
        parts.append(f"subcode={meta['subcode']}")
    if meta.get("type"):
        parts.append(f"type={meta['type']}")
    prefix = f"[{' '.join(parts)}] " if parts else ""
    return f"{prefix}{error}"[:2000]


def _backoff_seconds(attempt_index: int) -> float:
    if attempt_index < len(ATTEMPT_BACKOFF_SECONDS):
        return ATTEMPT_BACKOFF_SECONDS[attempt_index]
    return ATTEMPT_BACKOFF_SECONDS[-1]


def _process_one_row(
    api: ThreadsAPI,
    store: SupabaseStore,
    table: str,
    campaign_code: str | None,
    *,
    attempt_deadline_seconds: float,
    max_attempts: int,
    max_persisted_attempts: int,
    sleep: Any,
    clock: Any,
    now_iso: str,
    alert: Any = None,
) -> dict[str, Any]:
    """Publish a single due row with in-run transient retry + durable backoff.

    Returns the terminal publish_next result for the row selected at entry,
    augmented with ``requeued``, ``attempts``, ``persisted_attempts``,
    ``max_attempts_reached`` and ``terminal`` markers. A transient failure is
    persisted with a backoff gate and the run moves on — the row is retried by a
    later tick once its ``next_retry_at`` elapses, never re-selected in a tight
    loop, so it cannot starve other due rows. Never raises for an ordinary
    publish failure; raises only for unexpected store errors.
    """
    result = publish_next(api, store, table, campaign_code=campaign_code)
    status = result.get("status")
    if status != "failed":
        return result

    row_id = result.get("queue_id")
    error = result.get("error", "unknown error")
    result["attempts"] = 1
    if row_id is None:
        result["terminal"] = True
        return result

    # Read the persisted attempt count so retries survive across processes.
    prior_attempts = 0
    try:
        row = store.fetch_queue_row(table, row_id)
        if row:
            prior_attempts = int(row.get("attempt_count") or 0)
    except Exception:
        prior_attempts = 0
    persisted_attempts = prior_attempts + 1
    result["persisted_attempts"] = persisted_attempts
    attempted_at = _utc_now_iso()
    last_error = _format_last_error(result, error)

    # Permanent content rejection: never auto-requeue, fail for manual review.
    if is_manual_review_result(result):
        store.mark_post_failed(table, row_id, last_error)
        result["terminal"] = True
        result["manual_review"] = True
        if alert is not None:
            alert(store, table, row_id, result, persisted_attempts, last_error, reason="manual_review")
        return result

    # Max-attempt cap: stop retrying, mark failed, alert, let later rows run.
    if persisted_attempts >= max_persisted_attempts:
        store.mark_post_failed(table, row_id, last_error)
        result["terminal"] = True
        result["max_attempts_reached"] = True
        if alert is not None:
            alert(store, table, row_id, result, persisted_attempts, last_error, reason="max_attempts")
        return result

    # Transient: persist backoff and requeue so a later tick retries once the
    # backoff elapses — without starving other due rows.
    backoff = _persisted_backoff_seconds(persisted_attempts)
    next_retry_at = _iso_plus(backoff)
    if is_transient_publish_result(result):
        try:
            if requeue_failed_row(
                store,
                table,
                row_id,
                last_error,
                next_retry_at=next_retry_at,
                attempt_count=persisted_attempts,
                attempted_at=attempted_at,
            ):
                result["requeued"] = True
                result["next_retry_at"] = next_retry_at
        except Exception:
            # A failed requeue must not mask the original publish failure.
            result["requeued"] = False
    if not result.get("requeued"):
        result["terminal"] = True
    return result


def publish_next_with_recovery(
    api: ThreadsAPI,
    store: SupabaseStore,
    table: str,
    campaign_code: str | None = None,
    *,
    dry_run: bool = False,
    attempt_deadline_seconds: float = DEFAULT_ATTEMPT_DEADLINE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_persisted_attempts: int = DEFAULT_MAX_PERSISTED_ATTEMPTS,
    max_rows_per_run: int = DEFAULT_MAX_ROWS_PER_RUN,
    sleep: Any = time.sleep,
    clock: Any = time.monotonic,
    alert: Any = None,
) -> dict[str, Any]:
    """Publish due approved rows, isolating poisoned rows automatically.

    Backoff-aware selection (``next_retry_at``) means a repeatedly-failing row
    is never re-selected every cron tick, so it cannot starve later due rows.
    Each failed attempt is persisted (``attempt_count``/``last_attempt_at`` /
    ``next_retry_at``); after ``max_persisted_attempts`` the row is marked
    ``failed`` and alerted. Permanent content rejections (code 24/subcode
    4279009, over-length text) fail immediately for manual review.

    Processes up to ``max_rows_per_run`` eligible due rows per run, oldest-first
    and sequentially (never concurrently), to recover backlog safely.

    Returns a summary dict: ``status`` (posted|no_due_posts|failed),
    ``processed`` (list of per-row results), and the last ``result``.
    """
    if dry_run:
        return publish_next(api, store, table, campaign_code=campaign_code, dry_run=True)

    now_iso = _utc_now_iso()
    processed: list[dict[str, Any]] = []
    seen_ids: set = set()
    last_result: dict[str, Any] = {"status": "no_due_posts"}
    for _ in range(max(1, max_rows_per_run)):
        result = _process_one_row(
            api,
            store,
            table,
            campaign_code,
            attempt_deadline_seconds=attempt_deadline_seconds,
            max_attempts=max_attempts,
            max_persisted_attempts=max_persisted_attempts,
            sleep=sleep,
            clock=clock,
            now_iso=now_iso,
            alert=alert,
        )
        last_result = result
        status = result.get("status")
        if status in ("no_due_posts", "idle"):
            break
        # Defensive: never process the same queue row twice in one run. With the
        # real store a requeued row is hidden by next_retry_at, but guard anyway
        # so a store that ignores backoff can never cause a re-selection loop.
        qid = result.get("queue_id")
        if qid is not None and qid in seen_ids:
            break
        if qid is not None:
            seen_ids.add(qid)
        processed.append(result)
        # Stop the run on rate-limit so we do not hammer Meta; backoff persisted.
        if result.get("requeued") and is_rate_limited_publish_result(result):
            break
        # Continue to the next eligible due row otherwise (posted, terminal
        # failure, or a requeued-with-backoff row now yields its slot).

    if not processed:
        return {"status": "no_due_posts", "processed": [], "result": last_result}
    any_posted = any(r.get("status") == "posted" for r in processed)
    summary_status = "posted" if any_posted else processed[-1].get("status", "failed")
    summary: dict[str, Any] = {
        # Surface the last row's fields at top level for backward compatibility
        # with callers/tests that read status/queue_id/etc. from the return.
        **{k: v for k, v in processed[-1].items() if k not in {"processed"}},
        "processed": processed,
        "processed_count": len(processed),
        "result": processed[-1],
    }
    # The run-level status is authoritative; never let a trailing idle row
    # (no more due rows) downgrade a run that actually posted.
    summary["status"] = summary_status
    return summary
