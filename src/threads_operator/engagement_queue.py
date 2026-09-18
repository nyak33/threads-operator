"""Approval-gated engagement queue helpers.

Hermes may propose reply text, but only an explicit human approval transition
can make a reply executable. All reads/writes are scoped to store.account_key.
"""
from __future__ import annotations

from typing import Any

from .supabase_store import SupabaseStore
from .trend_urls import normalize_threads_post_url

ENGAGEMENT_TABLE = "threads_engagement_queue"
ENGAGEMENT_ACTIONS = frozenset({"reply", "repost", "like"})
ENGAGEMENT_STATUSES = frozenset(
    {
        "draft",
        "pending_approval",
        "approved",
        "executing",
        "posted",
        "rejected",
        "expired",
        "failed",
        "skipped",
    }
)


def _positive_id(value: Any, field: str = "id") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _nonblank(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-blank string")
    return value.strip()


def _source_post_id(value: Any) -> str:
    raw = _nonblank(value, "source_post_id")
    if not any(ch.isdigit() for ch in raw):
        raise ValueError("source_post_id must contain a numeric Threads id")
    if any(ch not in "0123456789:._-" for ch in raw):
        raise ValueError("source_post_id has invalid characters")
    return raw


def _score(value: float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("score must be numeric")
    score = float(value)
    if not 0 <= score <= 100:
        raise ValueError("score must be between 0 and 100")
    return score


def _account(store: SupabaseStore) -> str:
    return store._require_account_key()


def _url(store: SupabaseStore) -> str:
    return f"{store.base_url}/rest/v1/{ENGAGEMENT_TABLE}"


def _get(store: SupabaseStore, engagement_id: int) -> dict[str, Any] | None:
    account_key = _account(store)
    engagement_id = _positive_id(engagement_id, "engagement id")
    response = store.client.get(
        _url(store),
        headers=store._headers,
        params={
            "id": f"eq.{engagement_id}",
            "account_key": f"eq.{account_key}",
            "select": "*",
            "limit": "1",
        },
    )
    response.raise_for_status()
    rows = response.json() or []
    return rows[0] if rows else None


def list_actions(
    store: SupabaseStore,
    *,
    status: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    account_key = _account(store)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    if status is not None and status not in ENGAGEMENT_STATUSES:
        raise ValueError("invalid engagement status")
    params: dict[str, str] = {
        "account_key": f"eq.{account_key}",
        "select": "*",
        "order": "created_at.desc,id.desc",
        "limit": str(limit),
    }
    if status:
        params["status"] = f"eq.{status}"
    response = store.client.get(_url(store), headers=store._headers, params=params)
    response.raise_for_status()
    return [row for row in (response.json() or []) if isinstance(row, dict)]


def propose_reply(
    store: SupabaseStore,
    *,
    source_post_id: str,
    source_permalink: str,
    proposed_text: str,
    source_username: str | None = None,
    source_text: str | None = None,
    trend_candidate_id: int | None = None,
    score: float | int | None = None,
    reason: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    """Insert a pending-approval reply, deduped per account+post+action."""
    account_key = _account(store)
    permalink, parsed_username = normalize_threads_post_url(source_permalink)
    source_post_id = _source_post_id(source_post_id)
    proposed_text = _nonblank(proposed_text, "proposed_text")
    if trend_candidate_id is not None:
        _positive_id(trend_candidate_id, "trend_candidate_id")

    lookup = store.client.get(
        _url(store),
        headers=store._headers,
        params={
            "account_key": f"eq.{account_key}",
            "source_permalink": f"eq.{permalink}",
            "action": "eq.reply",
            "select": "id,status,proposed_text",
            "limit": "1",
        },
    )
    lookup.raise_for_status()
    existing = lookup.json() or []
    if existing:
        return {"result": "existing", "action": existing[0]}

    payload: dict[str, Any] = {
        "account_key": account_key,
        "trend_candidate_id": trend_candidate_id,
        "source_post_id": source_post_id,
        "source_permalink": permalink,
        "source_username": source_username or parsed_username,
        "source_text": source_text,
        "action": "reply",
        "proposed_text": proposed_text,
        "score": _score(score),
        "reason": reason,
        "status": "pending_approval",
        "approval_channel": "telebot",
        "expires_at": expires_at,
    }
    headers = {**store._headers, "Prefer": "return=representation"}
    response = store.client.post(_url(store), headers=headers, json=payload)
    if response.status_code == 409:
        # Lost a race against an identical proposal. Re-read once; never
        # recurse indefinitely if the REST read lags the unique constraint.
        reread = store.client.get(
            _url(store),
            headers=store._headers,
            params={
                "account_key": f"eq.{account_key}",
                "source_permalink": f"eq.{permalink}",
                "action": "eq.reply",
                "select": "id,status,proposed_text",
                "limit": "1",
            },
        )
        reread.raise_for_status()
        rows = reread.json() or []
        if rows:
            return {"result": "existing", "action": rows[0]}
        raise RuntimeError("engagement dedupe conflict but existing row was not readable")
    response.raise_for_status()
    rows = response.json() or []
    if not rows:
        raise ValueError("engagement insert returned no row")
    return {"result": "inserted", "action": rows[0]}


def _transition(
    store: SupabaseStore,
    engagement_id: int,
    *,
    expected_status: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    account_key = _account(store)
    engagement_id = _positive_id(engagement_id, "engagement id")
    headers = {**store._headers, "Prefer": "return=representation"}
    response = store.client.patch(
        _url(store),
        headers=headers,
        params={
            "id": f"eq.{engagement_id}",
            "account_key": f"eq.{account_key}",
            "status": f"eq.{expected_status}",
        },
        json=payload,
    )
    response.raise_for_status()
    rows = response.json() or []
    return rows[0] if rows else None


def edit_pending_reply(
    store: SupabaseStore,
    engagement_id: int,
    *,
    proposed_text: str,
) -> dict[str, Any] | None:
    return _transition(
        store,
        engagement_id,
        expected_status="pending_approval",
        payload={
            "proposed_text": _nonblank(proposed_text, "proposed_text"),
            "updated_at": store._utc_now(),
        },
    )


def approve_reply(
    store: SupabaseStore,
    engagement_id: int,
    *,
    approval_ref: str | None = None,
) -> dict[str, Any] | None:
    now = store._utc_now()
    return _transition(
        store,
        engagement_id,
        expected_status="pending_approval",
        payload={
            "status": "approved",
            "approved_at": now,
            "approval_channel": "telebot",
            "approval_ref": approval_ref,
            "updated_at": now,
        },
    )


def reject_reply(
    store: SupabaseStore,
    engagement_id: int,
    *,
    approval_ref: str | None = None,
) -> dict[str, Any] | None:
    now = store._utc_now()
    return _transition(
        store,
        engagement_id,
        expected_status="pending_approval",
        payload={
            "status": "rejected",
            "rejected_at": now,
            "approval_channel": "telebot",
            "approval_ref": approval_ref,
            "updated_at": now,
        },
    )


def claim_approved_reply(
    store: SupabaseStore,
    engagement_id: int,
) -> dict[str, Any] | None:
    now = store._utc_now()
    return _transition(
        store,
        engagement_id,
        expected_status="approved",
        payload={"status": "executing", "claimed_at": now, "updated_at": now},
    )


def mark_reply_posted(
    store: SupabaseStore,
    engagement_id: int,
    *,
    external_action_id: str,
) -> dict[str, Any] | None:
    now = store._utc_now()
    return _transition(
        store,
        engagement_id,
        expected_status="executing",
        payload={
            "status": "posted",
            "external_action_id": _nonblank(external_action_id, "external_action_id"),
            "executed_at": now,
            "last_error": None,
            "updated_at": now,
        },
    )


def mark_reply_failed(
    store: SupabaseStore,
    engagement_id: int,
    *,
    error: str,
) -> dict[str, Any] | None:
    now = store._utc_now()
    return _transition(
        store,
        engagement_id,
        expected_status="executing",
        payload={
            "status": "failed",
            "last_error": str(error)[:2000],
            "updated_at": now,
        },
    )
