"""DM opportunity lifecycle for Workflow B (own-post replies).

Manages the state machine for DM opportunities detected from own-post
replies. The lifecycle is:

    detected -> drafted -> awaiting_approval -> approved -> sending -> sent

Alternative terminal states:
    rejected, failed, expired, cancelled

This module only handles the DATA MODEL and state transitions. It does
NOT send any DM. The Telegram approval workflow (Task #2 continuation)
and the browser-based DM sender (Task #3) are separate layers that
consume this state machine.

Dedupe: one active DM opportunity per (account_key, from_username, root_post_id).
The unique constraint on threads_dm_opportunities enforces this at the DB level.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .reply_context import ReplyContext
from .reply_intent import IntentClassification

logger = logging.getLogger(__name__)

# --- State machine constants ---
DM_STATUS_DETECTED = "detected"
DM_STATUS_DRAFTED = "drafted"
DM_STATUS_AWAITING_APPROVAL = "awaiting_approval"
DM_STATUS_APPROVED = "approved"
DM_STATUS_SENDING = "sending"
DM_STATUS_SENT = "sent"
DM_STATUS_REJECTED = "rejected"
DM_STATUS_FAILED = "failed"
DM_STATUS_EXPIRED = "expired"
DM_STATUS_CANCELLED = "cancelled"

DM_ACTIVE_STATUSES = frozenset({
    DM_STATUS_DETECTED,
    DM_STATUS_DRAFTED,
    DM_STATUS_AWAITING_APPROVAL,
    DM_STATUS_APPROVED,
    DM_STATUS_SENDING,
})

DM_TERMINAL_STATUSES = frozenset({
    DM_STATUS_SENT,
    DM_STATUS_REJECTED,
    DM_STATUS_FAILED,
    DM_STATUS_EXPIRED,
    DM_STATUS_CANCELLED,
})

# Default expiry for DM opportunities awaiting approval
DM_DEFAULT_EXPIRY_HOURS = 72


@dataclass(frozen=True)
class DMOppotunityResult:
    """Result of a DM opportunity operation."""
    success: bool
    opportunity_id: int | None
    status: str | None
    error: str | None = None
    already_exists: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "opportunity_id": self.opportunity_id,
            "status": self.status,
            "error": self.error,
            "already_exists": self.already_exists,
        }


def create_dm_opportunity(
    *,
    store,
    account_key: str,
    context: ReplyContext,
    classification: IntentClassification,
    expiry_hours: int = DM_DEFAULT_EXPIRY_HOURS,
) -> DMOppotunityResult:
    """Create a DM opportunity from a classified reply.

    Args:
        store: Account-scoped Supabase store.
        account_key: The account this opportunity belongs to.
        context: The assembled ReplyContext.
        classification: The IntentClassification result.
        expiry_hours: Hours until the opportunity expires.

    Returns:
        DMOppotunityResult with the created opportunity ID or dedup info.
    """
    from .reply_intent import should_create_dm_opportunity

    # Fail-closed gate: only create if classification says so
    if not should_create_dm_opportunity(classification):
        return DMOppotunityResult(
            success=False,
            opportunity_id=None,
            status=None,
            error="Classification does not warrant a DM opportunity",
            already_exists=False,
        )

    # Validate identity
    if not account_key or store.account_key != account_key:
        return DMOppotunityResult(
            success=False,
            opportunity_id=None,
            status=None,
            error="Account mismatch — refusing to create cross-account opportunity",
        )

    if not context.root_post_id or not context.reply_username:
        return DMOppotunityResult(
            success=False,
            opportunity_id=None,
            status=None,
            error="Missing root_post_id or reply_username — cannot create DM opportunity",
        )

    # Resolve the persisted own-reply row first. Production schema requires
    # source_own_reply_id as a NOT NULL FK, so the opportunity insert must carry
    # the real row id rather than trying to link it only afterwards.
    source_row = store.get_own_reply_by_reply_id(context.reply_id)
    if (
        not source_row
        or source_row.get("account_key") != account_key
        or source_row.get("id") is None
    ):
        return DMOppotunityResult(
            success=False,
            opportunity_id=None,
            status=None,
            error="Persisted own-reply row missing or account mismatch",
        )
    try:
        source_own_reply_id = int(source_row["id"])
    except (TypeError, ValueError):
        return DMOppotunityResult(
            success=False,
            opportunity_id=None,
            status=None,
            error="Persisted own-reply row has invalid id",
        )

    # Compute expiry
    expires_at = datetime.now(timezone.utc) + timedelta(hours=expiry_hours)

    # Build the insert payload
    payload = {
        "account_key": account_key,
        "source_reply_id": context.reply_id,
        "source_own_reply_id": source_own_reply_id,
        "from_username": context.reply_username,
        "root_post_id": context.root_post_id,
        "root_post_permalink": context.root_post_permalink,
        "root_post_text": context.root_post_text,
        "reply_text": context.reply_text,
        "reply_permalink": None,  # Not always available
        "cta_matched": context.post_cta,
        "intent": classification.intent,
        "lead_score": classification.lead_score,
        "status": DM_STATUS_DETECTED,
        "expires_at": expires_at.isoformat(),
    }

    try:
        # Use the store's insert method (idempotent via unique constraint)
        row = store.insert_dm_opportunity(payload)
        if row is None:
            # Unique constraint violation — already exists
            existing = store.get_dm_opportunity_by_dedupe(
                account_key=account_key,
                from_username=context.reply_username,
                root_post_id=context.root_post_id,
            )
            if existing:
                return DMOppotunityResult(
                    success=True,
                    opportunity_id=int(existing["id"]),
                    status=existing["status"],
                    already_exists=True,
                )
            return DMOppotunityResult(
                success=False,
                opportunity_id=None,
                status=None,
                error="Insert returned None but no existing row found",
            )

        return DMOppotunityResult(
            success=True,
            opportunity_id=int(row["id"]),
            status=row["status"],
            already_exists=False,
        )

    except Exception as exc:
        logger.error("Failed to create DM opportunity: %s", exc)
        # Check if it's a unique constraint violation (race condition)
        existing = store.get_dm_opportunity_by_dedupe(
            account_key=account_key,
            from_username=context.reply_username,
            root_post_id=context.root_post_id,
        )
        if existing:
            return DMOppotunityResult(
                success=True,
                opportunity_id=int(existing["id"]),
                status=existing["status"],
                already_exists=True,
            )
        return DMOppotunityResult(
            success=False,
            opportunity_id=None,
            status=None,
            error=f"Insert failed: {type(exc).__name__}: {exc}",
        )


def link_reply_to_dm_opportunity(
    *,
    store,
    reply_row_id: int,
    dm_opportunity_id: int,
) -> bool:
    """Link an own_reply_engagement row to its DM opportunity.

    Args:
        store: Account-scoped Supabase store.
        reply_row_id: ID of the threads_own_reply_engagement row.
        dm_opportunity_id: ID of the threads_dm_opportunities row.

    Returns:
        True if the link was updated, False otherwise.
    """
    try:
        result = store.update_own_reply(
            row_id=reply_row_id,
            fields={"dm_opportunity_id": dm_opportunity_id},
        )
        return result is not None
    except Exception as exc:
        logger.error("Failed to link reply %d to DM opportunity %d: %s",
                     reply_row_id, dm_opportunity_id, exc)
        return False


def transition_dm_opportunity(
    *,
    store,
    opportunity_id: int,
    from_status: str | tuple[str, ...],
    to_status: str,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Transition a DM opportunity to a new state (CAS-guarded).

    Args:
        store: Account-scoped Supabase store.
        opportunity_id: ID of the DM opportunity row.
        from_status: Expected current status (or tuple of allowed statuses).
        to_status: Target status.
        fields: Additional fields to update.

    Returns:
        The updated row, or None if the transition was rejected.
    """
    return store.transition_dm_opportunity(
        opportunity_id=opportunity_id,
        from_status=from_status,
        to_status=to_status,
        fields=fields or {},
    )


def get_dm_opportunity(store, opportunity_id: int) -> dict[str, Any] | None:
    """Fetch one DM opportunity by ID."""
    return store.get_dm_opportunity(opportunity_id)


def list_dm_opportunities(
    store,
    *,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List DM opportunities, optionally filtered by status."""
    return store.list_dm_opportunities(status=status, limit=limit)


def expire_stale_opportunities(store, *, dry_run: bool = False) -> dict[str, Any]:
    """Expire DM opportunities past their expiry time.

    Args:
        store: Account-scoped Supabase store.
        dry_run: If True, only report what would be expired.

    Returns:
        Dict with count of expired opportunities and their IDs.
    """
    now = datetime.now(timezone.utc).isoformat()
    stale = store.find_expired_dm_opportunities(now=now, limit=100)

    if dry_run:
        return {
            "dry_run": True,
            "stale_count": len(stale),
            "ids": [row.get("id") for row in stale],
        }

    expired = 0
    errors = []
    for row in stale:
        try:
            result = transition_dm_opportunity(
                store=store,
                opportunity_id=int(row["id"]),
                from_status=tuple(sorted(DM_ACTIVE_STATUSES)),
                to_status=DM_STATUS_EXPIRED,
                fields={"expired_at": now},
            )
            if result is not None:
                expired += 1
        except Exception as exc:
            errors.append({"id": row.get("id"), "error": str(exc)})

    return {
        "dry_run": False,
        "expired": expired,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Task 2C — DM approval lifecycle helpers
# ---------------------------------------------------------------------------
#
# These helpers own the approval half of the lifecycle:
#
#   detected -> drafted -> awaiting_approval -> approved
#
# plus edit-in-place on ``awaiting_approval`` and the Telegram-card bookkeeping.
# Every transition is CAS-guarded and account-scoped via the store; a wrong
# account or a stale precondition simply yields ``None`` (fail closed).
# None of these functions send a DM — Task 2D consumes only ``approved`` rows.

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_expired(row: dict[str, Any], *, now: datetime | None = None) -> bool:
    """True if the opportunity is past its ``expires_at`` (fail closed)."""
    raw = row.get("expires_at")
    if not raw:
        return False
    try:
        expiry = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False  # unparseable -> don't block, but don't approve either
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) >= expiry


def draft_dm_opportunity(
    *, store, opportunity_id: int, draft_text: str
) -> dict[str, Any] | None:
    """Persist the exact generated draft and move detected -> drafted.

    Idempotent-ish: re-drafting an already drafted row refreshes the draft
    (watchdog retry after a card was never sent). Refuses on rows that have
    moved past drafting (fail closed)."""
    if not draft_text or not draft_text.strip():
        raise ValueError("draft_text must not be empty")
    return store.transition_dm_opportunity(
        opportunity_id=opportunity_id,
        from_status=(DM_STATUS_DETECTED, DM_STATUS_DRAFTED),
        to_status=DM_STATUS_DRAFTED,
        fields={"dm_draft_text": draft_text, "drafted_at": _utc_now_iso()},
    )


def mark_dm_awaiting_approval(
    *, store, opportunity_id: int, approval_ref: str | None = None
) -> dict[str, Any] | None:
    """Move drafted -> awaiting_approval and record the approval channel."""
    row = store.get_dm_opportunity(opportunity_id)
    if not row or not row.get("dm_draft_text"):
        return None  # fail closed: never send a card without a persisted draft
    fields: dict[str, Any] = {"approval_channel": "telegram"}
    if approval_ref:
        fields["approval_ref"] = approval_ref
    return store.transition_dm_opportunity(
        opportunity_id=opportunity_id,
        from_status=DM_STATUS_DRAFTED,
        to_status=DM_STATUS_AWAITING_APPROVAL,
        fields=fields,
    )


def approve_dm_opportunity(
    *, store, opportunity_id: int, approved_by: str | None = None
) -> dict[str, Any] | None:
    """Approve awaiting_approval -> approved. Snapshot the exact draft as
    ``dm_approved_text``. Refuses expired opportunities (fail closed)."""
    row = store.get_dm_opportunity(opportunity_id)
    if not row:
        return None
    if is_expired(row):
        return None
    draft = row.get("dm_draft_text")
    if not draft or not draft.strip():
        return None
    fields: dict[str, Any] = {
        "dm_approved_text": draft,
        "approved_at": _utc_now_iso(),
    }
    if approved_by:
        fields["approved_by"] = approved_by
    return store.transition_dm_opportunity(
        opportunity_id=opportunity_id,
        from_status=DM_STATUS_AWAITING_APPROVAL,
        to_status=DM_STATUS_APPROVED,
        fields=fields,
    )


def reject_dm_opportunity(
    *, store, opportunity_id: int, reason: str | None = None
) -> dict[str, Any] | None:
    """Reject from any approval-stage state -> rejected (terminal)."""
    fields: dict[str, Any] = {"rejected_at": _utc_now_iso()}
    if reason:
        fields["reject_reason"] = reason
    return store.transition_dm_opportunity(
        opportunity_id=opportunity_id,
        from_status=(DM_STATUS_DETECTED, DM_STATUS_DRAFTED, DM_STATUS_AWAITING_APPROVAL),
        to_status=DM_STATUS_REJECTED,
        fields=fields,
    )


def edit_dm_opportunity_draft(
    *, store, opportunity_id: int, edited_text: str
) -> dict[str, Any] | None:
    """Replace the draft while staying in awaiting_approval.

    Editing NEVER approves. Refuses expired opportunities and empty text
    (fail closed)."""
    if not edited_text or not edited_text.strip():
        raise ValueError("edited_text must not be empty")
    row = store.get_dm_opportunity(opportunity_id)
    if not row or is_expired(row):
        return None
    return store.transition_dm_opportunity(
        opportunity_id=opportunity_id,
        from_status=DM_STATUS_AWAITING_APPROVAL,
        to_status=DM_STATUS_AWAITING_APPROVAL,
        fields={"dm_draft_text": edited_text, "edited_at": _utc_now_iso()},
    )


def record_dm_approval_card_sent(
    *, store, opportunity_id: int, message_ref: str
) -> dict[str, Any] | None:
    """Stamp the Telegram message ref (``chat_id:message_id``) for the active
    approval card. Only valid while awaiting_approval — a row that already
    carries a card ref is not re-stamped by the watchdog (no duplicate cards)."""
    return store.transition_dm_opportunity(
        opportunity_id=opportunity_id,
        from_status=DM_STATUS_AWAITING_APPROVAL,
        to_status=DM_STATUS_AWAITING_APPROVAL,
        fields={"approval_message_ref": message_ref},
    )


def list_dm_opportunities_needing_card(
    store, *, limit: int = 50
) -> list[dict[str, Any]]:
    """Opportunities the watchdog must act on:

    * ``detected``            -> needs drafting
    * ``drafted``             -> needs a card (awaiting_approval + card)
    * ``awaiting_approval``   -> needs a card only if ``approval_message_ref``
                                  is not set (dispatch retry must not duplicate
                                  an active card)
    """
    rows = store.list_dm_opportunities(
        status=None, limit=limit * 3  # over-fetch then filter deterministically
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        status = row.get("status")
        if status in (DM_STATUS_DETECTED, DM_STATUS_DRAFTED):
            out.append(row)
        elif status == DM_STATUS_AWAITING_APPROVAL and not row.get("approval_message_ref"):
            out.append(row)
    return out[:limit]
