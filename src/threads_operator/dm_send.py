"""Task 2D — safe browser-based Threads DM sender.

Consumes ONLY `threads_dm_opportunities` rows already in ``status = 'approved'``
and sends the exact persisted approved text to the exact intended Threads user
via the persistent authenticated browser (``browser_cdp.ActivityBrowser``).

Threads Operator owns the entire send path: eligibility, atomic claim, account
+ recipient verification, exact-text enforcement, send confirmation, dedup,
reconciliation, audit and Supabase persistence. No model touches the text after
``approved``. The browser is only ever commanded with the persisted value.

Fail-closed invariants (wrong recipient / duplicate send are the worst outcomes):

  * Only ``approved`` rows with approved text + target username are eligible.
  * The browser's logged-in username must equal the opportunity's account.
  * Recipient resolution requires an EXACT canonical username match; a partial
    or ambiguous result never sends.
  * The composer text must equal the approved text before Send; a mismatch
    aborts BEFORE the click.
  * Clicking Send is NOT proof of delivery; a confirmation read decides
    ``sent``. An unreadable confirmation goes to ``send_uncertain`` — never
    silently back to ``approved`` and never auto-resends.
  * A confirmed ``sent`` row can never be sent again.

The live page is abstracted behind a small set of primitives so the whole flow
is deterministically testable with a fake page (see tests/test_dm_send.py).
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

# Failure categories (structured). The live browser adapter maps its DOM/
# navigation problems onto these; the deterministic sender logic also raises
# them directly for account/recipient/text/confirmation failures.
CAT_BROWSER_UNAVAILABLE = "browser_unavailable"
CAT_LOGIN_REQUIRED = "login_required"
CAT_ACCOUNT_MISMATCH = "account_mismatch"
CAT_RECIPIENT_NOT_FOUND = "recipient_not_found"
CAT_RECIPIENT_AMBIGUOUS = "recipient_ambiguous"
CAT_COMPOSER_NOT_FOUND = "composer_not_found"
CAT_TEXT_MISMATCH = "text_verification_failed"
CAT_SEND_UI_UNAVAILABLE = "send_ui_unavailable"
CAT_PLATFORM_RESTRICTED = "platform_dm_restricted"
CAT_CONFIRMATION_FAILED = "confirmation_failed"
CAT_SEND_UNCERTAIN = "send_uncertain"
CAT_SESSION_CHALLENGE = "session_challenge"
CAT_UNEXPECTED_UI = "unexpected_ui"

# Challenges that must stop execution (never bypassed).
_CHALLENGE_MAP = {
    "login": CAT_LOGIN_REQUIRED,
    "2fa": CAT_SESSION_CHALLENGE,
    "challenge": CAT_SESSION_CHALLENGE,
    "captcha": CAT_SESSION_CHALLENGE,
    "checkpoint": CAT_SESSION_CHALLENGE,
}

STATUS_APPROVED = "approved"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_SEND_UNCERTAIN = "send_uncertain"
STATUS_FAILED = "failed"


class PlatformDMRestricted(Exception):
    """Threads blocked the DM for privacy/platform reasons — clean failure."""


class SendAborted(Exception):
    """Internal control-flow: abort with a structured failure category."""

    def __init__(self, category: str, detail: str = ""):
        super().__init__(detail or category)
        self.category = category
        self.detail = detail


class DMPage(Protocol):
    """The minimal live-page surface the sender is allowed to drive.

    A real implementation wraps ``browser_cdp`` navigation + Runtime.evaluate;
    tests substitute a deterministic fake. Method names match the recon-grounded
    Threads web DM flow.
    """

    def current_logged_in_username(self) -> str | None: ...
    def detect_challenge(self) -> str | None: ...
    def search_recipients(self, query: str) -> list[tuple[str, str]]: ...
    def open_conversation(self, canonical_username: str) -> str | None: ...
    def insert_text(self, text: str) -> None: ...
    def read_composer(self) -> str: ...
    def click_send(self) -> None: ...
    def confirm_latest_outgoing(self, expected_text: str) -> dict | None: ...
    def recent_outgoing_contains(self, expected_text: str) -> bool | None: ...


@dataclass
class EligibilityResult:
    ok: bool
    reason: str = ""


@dataclass
class SendResult:
    ok: bool
    opportunity_id: int = 0
    account: str = ""
    target_username: str = ""
    attempt: int = 0
    failure_category: str | None = None
    detail: str = ""
    confirmation_ref: str | None = None
    text_hash: str = ""
    sent: bool = False
    uncertain: bool = False


@dataclass
class ReconcileResult:
    resolved: bool
    can_retry: bool = False
    outcome: str = ""          # "sent" | "approved" | "send_uncertain"
    detail: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _norm_username(u: str | None) -> str:
    return (u or "").strip().lstrip("@").lower()


# --------------------------------------------------------------------------- eligibility
def check_eligibility(store, opportunity_id: int) -> EligibilityResult:
    """Fail-closed gate: only an approved row with approved text + username
    that has never been sent and is not expired/cancelled is eligible."""
    row = store.get_dm_opportunity(opportunity_id)
    if row is None:
        return EligibilityResult(False, "not found (or not this account)")
    if row.get("status") != STATUS_APPROVED:
        return EligibilityResult(False, f"status is {row.get('status')!r}, not 'approved'")
    if not (row.get("dm_approved_text") or "").strip():
        return EligibilityResult(False, "no approved text")
    if not _norm_username(row.get("target_threads_username")):
        return EligibilityResult(False, "no target username")
    if row.get("sent_at"):
        return EligibilityResult(False, "already sent")
    exp = row.get("expires_at")
    if exp:
        try:
            if datetime.fromisoformat(str(exp).replace("Z", "+00:00")) < datetime.now(timezone.utc):
                return EligibilityResult(False, "expired")
        except ValueError:
            return EligibilityResult(False, "unparseable expires_at")
    return EligibilityResult(True)


# --------------------------------------------------------------------------- atomic claim
def claim_dm_opportunity(store, opportunity_id: int, worker_id: str) -> dict | None:
    """Atomically claim ONE approved row: CAS approved -> sending, stamping a
    unique claim_id, claimed_at, last_attempt_at and incrementing attempt_count.

    The CAS is conditioned on status=eq.approved, so a competing worker that
    already moved the row to sending matches zero rows and gets None — no two
    workers can hold the same claim.
    """
    row = store.get_dm_opportunity(opportunity_id)
    if row is None or row.get("status") != STATUS_APPROVED:
        return None
    claim_id = uuid.uuid4().hex
    moved = store.transition_dm_opportunity(
        opportunity_id=opportunity_id,
        from_status=STATUS_APPROVED,
        to_status=STATUS_SENDING,
        fields={
            "claim_id": claim_id,
            "claimed_at": _utc_now(),
            "last_attempt_at": _utc_now(),
            "attempt_count": int(row.get("attempt_count") or 0) + 1,
        },
    )
    return moved


# --------------------------------------------------------------------------- helpers
def _verify_account(page: DMPage, expected_account: str) -> None:
    who = page.current_logged_in_username()
    if not who or _norm_username(who) != _norm_username(expected_account):
        raise SendAborted(CAT_ACCOUNT_MISMATCH,
                          f"browser account {who!r} != opportunity account {expected_account!r}")


def _check_challenge(page: DMPage) -> None:
    challenge = page.detect_challenge()
    if challenge:
        category = _CHALLENGE_MAP.get(str(challenge).lower(), CAT_SESSION_CHALLENGE)
        raise SendAborted(category, f"browser challenge: {challenge}")


def _resolve_recipient(page: DMPage, target_username: str) -> str:
    """Return the exact canonical username, or raise not-found / ambiguous."""
    target = _norm_username(target_username)
    rows = page.search_recipients(target)
    exact = [u for (u, _disp) in rows if _norm_username(u) == target]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise SendAborted(CAT_RECIPIENT_AMBIGUOUS,
                          f"{len(exact)} results match {target_username!r} exactly")
    raise SendAborted(CAT_RECIPIENT_NOT_FOUND,
                      f"no exact username match for {target_username!r}")


def _insert_and_verify_text(page: DMPage, approved_text: str) -> None:
    page.insert_text(approved_text)
    composer = page.read_composer() or ""
    # Normalize only browser-induced representation differences (trailing
    # whitespace/newlines the contenteditable adds). The deterministic value is
    # the persisted approved text; we never transform it.
    if composer.strip() != approved_text.strip():
        raise SendAborted(CAT_TEXT_MISMATCH,
                          "composer text does not equal approved text; aborting before Send")


# --------------------------------------------------------------------------- send
def send_dm_opportunity(
    store,
    opportunity_id: int,
    *,
    page: DMPage,
    worker_id: str,
) -> SendResult:
    """Claim and send one approved DM opportunity through the browser page.

    Deterministic given the page; all branching is fail-closed. Never sends a
    non-approved row, never sends to an unverified recipient, never sends text
    that differs from the persisted approved text, and never resends a sent row.
    """
    elig = check_eligibility(store, opportunity_id)
    base = SendResult(ok=False, opportunity_id=opportunity_id)
    if not elig.ok:
        base.failure_category = "not_eligible"
        base.detail = elig.reason
        return base

    row = store.get_dm_opportunity(opportunity_id)
    account = row.get("account_key") or ""
    target = row.get("target_threads_username") or ""
    approved_text = row.get("dm_approved_text") or ""
    base.account = account
    base.target_username = target
    base.text_hash = text_hash(approved_text)

    claim = claim_dm_opportunity(store, opportunity_id, worker_id)
    if claim is None:
        base.failure_category = "claim_failed"
        base.detail = "could not atomically claim (already claimed or state changed)"
        return base
    base.attempt = int(claim.get("attempt_count") or 1)

    try:
        _check_challenge(page)
        _verify_account(page, account)
        canonical = _resolve_recipient(page, target)
        try:
            thread_id = page.open_conversation(canonical)
        except PlatformDMRestricted as exc:
            raise SendAborted(CAT_PLATFORM_RESTRICTED, str(exc)) from exc
        if thread_id:
            base.confirmation_ref = str(thread_id)
        _insert_and_verify_text(page, approved_text)
        # Point of no return: the click. Confirmation decides the outcome.
        page.click_send()
        evidence = page.confirm_latest_outgoing(approved_text)
        if evidence:
            ref = str(evidence.get("thread_id") or base.confirmation_ref or "")
            _mark_sent(store, opportunity_id, from_status=STATUS_SENDING,
                       confirmation_ref=ref,
                       evidence=str(evidence.get("evidence") or "latest_outgoing_bubble"),
                       text_hash=base.text_hash)
            base.ok = True
            base.sent = True
            if ref:
                base.confirmation_ref = ref
            return base
        # Clicked but could not confirm -> uncertain. Do NOT auto-resend.
        _mark_uncertain(store, opportunity_id, base.text_hash,
                        "send clicked but confirmation could not be read")
        base.uncertain = True
        base.failure_category = CAT_SEND_UNCERTAIN
        base.detail = "send clicked; delivery confirmation unavailable — reconciliation required"
        return base
    except SendAborted as exc:
        _mark_failed(store, opportunity_id, exc.category, exc.detail, base.text_hash)
        base.failure_category = exc.category
        base.detail = exc.detail
        return base


def _mark_sent(store, oid, *, from_status, confirmation_ref, evidence, text_hash):
    store.transition_dm_opportunity(
        opportunity_id=oid, from_status=from_status, to_status=STATUS_SENT,
        fields={
            "sent_at": _utc_now(),
            "external_dm_id": confirmation_ref or None,
            "confirmation_ref": confirmation_ref or None,
            "confirmation_evidence": evidence,
            "sent_text_hash": text_hash,
            "last_error": None,
            "failure_category": None,
        },
    )


def _mark_uncertain(store, oid, text_hash, detail):
    store.transition_dm_opportunity(
        opportunity_id=oid, from_status=STATUS_SENDING, to_status=STATUS_SEND_UNCERTAIN,
        fields={
            "sent_text_hash": text_hash,
            "failure_category": CAT_SEND_UNCERTAIN,
            "last_error": detail,
            "last_attempt_at": _utc_now(),
        },
    )


def _mark_failed(store, oid, category, detail, text_hash):
    # Recoverable pre-send failures return to approved for a bounded retry;
    # deterministic identity/text/account failures stay failed (no safe retry).
    recoverable = category in (
        CAT_BROWSER_UNAVAILABLE, CAT_CONFIRMATION_FAILED, CAT_UNEXPECTED_UI,
        CAT_SEND_UI_UNAVAILABLE, CAT_COMPOSER_NOT_FOUND,
    )
    to_status = STATUS_APPROVED if recoverable else STATUS_FAILED
    store.transition_dm_opportunity(
        opportunity_id=oid,
        from_status=(STATUS_SENDING, STATUS_APPROVED),
        to_status=to_status,
        fields={
            "failure_category": category,
            "last_error": detail,
            "sent_text_hash": text_hash,
            "last_attempt_at": _utc_now(),
        },
    )


# --------------------------------------------------------------------------- reconciliation
def reconcile_dm_opportunity(store, opportunity_id: int, *, page: DMPage) -> ReconcileResult:
    """Resolve a send_uncertain row deterministically.

      * exact approved message already present as a recent outgoing  -> sent
      * confidently absent                                            -> approved (controlled retry)
      * still cannot determine                                        -> stay send_uncertain
    Only inspects enough recent outgoing context; never resends here.
    """
    row = store.get_dm_opportunity(opportunity_id)
    if row is None:
        return ReconcileResult(False, outcome="missing", detail="not found")
    if row.get("status") != STATUS_SEND_UNCERTAIN:
        return ReconcileResult(False, outcome=str(row.get("status")),
                               detail="not in send_uncertain")
    approved_text = row.get("dm_approved_text") or ""
    present = page.recent_outgoing_contains(approved_text)
    thash = text_hash(approved_text)
    if present is True:
        _mark_sent(store, opportunity_id, from_status=STATUS_SEND_UNCERTAIN,
                   confirmation_ref=row.get("confirmation_ref") or row.get("external_dm_id") or "",
                   evidence="reconcile_recent_outgoing", text_hash=thash)
        return ReconcileResult(True, outcome="sent", detail="exact message found in conversation")
    if present is False:
        store.transition_dm_opportunity(
            opportunity_id=opportunity_id, from_status=STATUS_SEND_UNCERTAIN,
            to_status=STATUS_APPROVED,
            fields={"failure_category": None, "last_error": None,
                    "last_attempt_at": _utc_now()},
        )
        return ReconcileResult(True, can_retry=True, outcome="approved",
                               detail="message confidently absent; eligible for controlled retry")
    return ReconcileResult(False, outcome="send_uncertain",
                           detail="still cannot determine delivery; manual action required")
