"""RED acceptance tests: Task 2D — safe browser-based Threads DM sender.

Real Threads web DM flow (grounded by read-only recon 2026-09-25, profile
~/.threads-operator/browser-profiles/syaqir, Chromium on :43399):

  * DM surface: https://www.threads.com/messages  (NOT /direct/... — those 404)
  * First visit shows an intro dialog "Direct messages have arrived on web"
    with a "Continue" button -> click to reach the inbox.
  * Recipient search = <input placeholder="Search">; typing filters the list.
  * Each recipient row = <div role="button"> whose innerText first line is the
    EXACT username, second line the display name. Exact match == first line.
  * Selecting a recipient navigates to /messages/t/<thread_id> and reveals the
    composer = <div contenteditable="true" role="textbox">.
  * The conversation URL /messages/t/<thread_id> is the external_dm_id.

The deterministic value is ALWAYS the persisted approved text. The browser is
orchestrated through a script of steps against a mocked page; no live Threads
is needed for the regression suite.
"""
import json
import pathlib

import httpx
import pytest

from threads_operator.supabase_store import SupabaseStore

ACCOUNT = "syaqir"
SERVICE_KEY = "service-KEY-not-real"
BASE = "https://supabase.test"
REST = f"{BASE}/rest/v1"
TABLE = "threads_dm_opportunities"
OID = 42
TARGET = "hanisahnorazman"
APPROVED_TEXT = "salam, kita ni belakang kira — jom DM sikit"


# --------------------------------------------------------------------------- harness
def seed_row(status="approved", **over):
    row = {
        "id": OID,
        "account_key": ACCOUNT,
        "source": "reply",
        "source_id": 501,
        "target_threads_user_id": None,
        "target_threads_username": TARGET,
        "status": status,
        "dm_draft_text": APPROVED_TEXT,
        "dm_approved_text": APPROVED_TEXT,
        "approval_ref": "79553451:5590",
        "detected_at": "2026-09-25T02:00:00+00:00",
        "expires_at": None,
        "claimed_at": None,
        "sent_at": None,
        "external_dm_id": None,
        "last_error": None,
        "attempt_count": 0,
        "lead_reason": "asked to DM",
        "reply_context": {"reply_text": "boleh DM?"},
        "created_at": "2026-09-25T01:00:00+00:00",
        "updated_at": "2026-09-25T02:00:00+00:00",
    }
    row.update(over)
    return row


class FakePostgREST:
    """Minimal PostgREST stand-in for the DM table with CAS support."""

    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.patches: list[dict] = []
        self.gets = 0

    def _match(self, row, params):
        for key, val in params.items():
            if key in ("select", "order", "limit", "offset"):
                continue
            col = key
            # PostgREST puts the operator in the VALUE:  id=eq.42, status=in.(a,b)
            op, _, expect = val.partition(".")
            if not expect:            # no operator -> plain equality (id=42)
                op, expect = "eq", val
            cur = row.get(col)
            if op == "eq":
                if expect in ("null", ""):
                    if cur is not None:
                        return False
                elif str(cur) != expect:
                    return False
            elif op == "in":
                vals = expect.strip("()").split(",")
                if str(cur) not in vals:
                    return False
            elif op == "not":
                if expect.startswith("in."):
                    vals = expect[3:].strip("()").split(",")
                    if str(cur) in vals:
                        return False
                elif expect.startswith("is.") and expect[3:] == "null":
                    if cur is None:
                        return False
                elif expect.startswith("eq."):
                    if str(cur) == expect[3:]:
                        return False
            # unknown ops: be permissive
        return True

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.gets += 1
        params = dict(request.url.params)
        if request.method == "GET":
            matched = [dict(r) for r in self.rows.values() if self._match(r, params)]
            limit = int(params.get("limit", len(matched) or 1))
            matched = matched[:limit]
            return httpx.Response(200, json=matched)
        if request.method == "PATCH":
            body = json.loads(request.content.decode() or "{}")
            updated = []
            for rid, row in self.rows.items():
                if self._match(row, params):
                    row.update(body)
                    updated.append(dict(row))
            self.patches.append({"params": params, "body": body, "updated": len(updated)})
            if not updated:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=updated)
        if request.method == "POST":
            body = json.loads(request.content.decode() or "{}")
            nid = max(self.rows) + 1 if self.rows else 1
            body.setdefault("id", nid)
            self.rows[body["id"]] = dict(body)
            return httpx.Response(201, json=[dict(body)])
        return httpx.Response(405, json={"error": "method"})


def make_store(rows):
    fake = FakePostgREST(rows)

    def handler(request):
        if TABLE in str(request.url):
            return fake.handle(request)
        return httpx.Response(404, json={"error": "no table"})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE)
    store = SupabaseStore(BASE, SERVICE_KEY, client=client, account_key=ACCOUNT)
    return store, fake


def patch_transport(store, fake):
    """Re-point an already-built store's client at the fake (no-op helper kept
    so tests read uniformly; the store was already built with the fake client)."""
    return store


# --------------------------------------------------------------------------- fake browser page
class FakePage:
    """Deterministic stand-in for the live Threads page. The sender drives a
    script of named steps against this; tests script the page's responses."""

    def __init__(self, *, logged_in_user=ACCOUNT, challenge=None, recipients=None,
                 composer_accept=True, confirm_send=True, thread_id="1290271635938149"):
        self.logged_in_user = logged_in_user
        self.challenge = challenge          # e.g. "captcha" | "login" | "2fa" | None
        self.recipients = recipients if recipients is not None else [TARGET]
        self.composer_accept = composer_accept
        self.confirm_send = confirm_send
        self.thread_id = thread_id
        self.calls: list[tuple] = []
        self.composer_text = ""
        self.sent_messages: list[str] = []
        self.selected_recipient = None

    # --- the primitives the sender is allowed to use ---
    def current_logged_in_username(self):
        self.calls.append(("whoami",))
        return self.logged_in_user

    def detect_challenge(self):
        self.calls.append(("detect_challenge",))
        return self.challenge

    def search_recipients(self, query):
        self.calls.append(("search", query))
        # rows: list of (canonical_username, display_name)
        return [(u, u) for u in self.recipients]

    def open_conversation(self, canonical_username):
        self.calls.append(("open", canonical_username))
        self.selected_recipient = canonical_username
        return self.thread_id

    def insert_text(self, text):
        self.calls.append(("insert", text))
        if self.composer_accept:
            self.composer_text = text
        else:
            self.composer_text = text + " extra"  # browser changes actual content

    def read_composer(self):
        self.calls.append(("read_composer",))
        return self.composer_text

    def click_send(self):
        self.calls.append(("click_send",))
        if self.confirm_send:
            self.sent_messages.append(self.composer_text)

    def confirm_latest_outgoing(self, expected_text):
        self.calls.append(("confirm", expected_text))
        if not self.confirm_send:
            return None  # ambiguous: cannot read confirmation
        if self.sent_messages and self.sent_messages[-1] == expected_text:
            return {"thread_id": self.thread_id, "evidence": "latest_outgoing_bubble"}
        return None

    def recent_outgoing_contains(self, expected_text):
        self.calls.append(("reconcile", expected_text))
        return expected_text in self.sent_messages


# ===========================================================================
# 1. ELIGIBILITY — only approved rows, fail closed
# ===========================================================================
def test_only_approved_is_eligible():
    from threads_operator import dm_send
    for st in ("detected", "drafted", "awaiting_approval", "rejected", "cancelled", "sent", "sending"):
        store, fake = make_store([seed_row(status=st)])
        patch_transport(store, fake)
        assert dm_send.check_eligibility(store, OID).ok is False, st


def test_approved_with_text_and_username_is_eligible():
    from threads_operator import dm_send
    store, fake = make_store([seed_row()])
    patch_transport(store, fake)
    res = dm_send.check_eligibility(store, OID)
    assert res.ok is True


def test_missing_approved_text_fails_closed():
    from threads_operator import dm_send
    store, fake = make_store([seed_row(dm_approved_text=None)])
    patch_transport(store, fake)
    assert dm_send.check_eligibility(store, OID).ok is False


def test_missing_username_fails_closed():
    from threads_operator import dm_send
    store, fake = make_store([seed_row(target_threads_username=None)])
    patch_transport(store, fake)
    assert dm_send.check_eligibility(store, OID).ok is False


def test_already_sent_is_not_eligible():
    from threads_operator import dm_send
    store, fake = make_store([seed_row(sent_at="2026-09-25T03:00:00+00:00")])
    patch_transport(store, fake)
    assert dm_send.check_eligibility(store, OID).ok is False


def test_expired_is_not_eligible():
    from threads_operator import dm_send
    store, fake = make_store([seed_row(expires_at="2000-01-01T00:00:00+00:00")])
    patch_transport(store, fake)
    assert dm_send.check_eligibility(store, OID).ok is False


def test_wrong_account_row_not_visible():
    from threads_operator import dm_send
    store, fake = make_store([seed_row(account_key="other")])
    patch_transport(store, fake)
    assert dm_send.check_eligibility(store, OID).ok is False


# ===========================================================================
# 2. ATOMIC CLAIM
# ===========================================================================
def test_claim_moves_approved_to_sending_with_claim_id():
    from threads_operator import dm_send
    store, fake = make_store([seed_row()])
    patch_transport(store, fake)
    claim = dm_send.claim_dm_opportunity(store, OID, worker_id="w1")
    assert claim is not None
    assert claim["status"] == "sending"
    assert claim.get("claim_id")
    assert claim.get("claimed_at")
    assert claim.get("attempt_count") == 1


def test_second_claim_is_prevented():
    from threads_operator import dm_send
    store, fake = make_store([seed_row()])
    patch_transport(store, fake)
    first = dm_send.claim_dm_opportunity(store, OID, worker_id="w1")
    assert first is not None
    # row is now 'sending'; a competing worker's CAS on approved must match nothing
    second = dm_send.claim_dm_opportunity(store, OID, worker_id="w2")
    assert second is None


def test_claim_uses_cas_on_approved_status():
    from threads_operator import dm_send
    store, fake = make_store([seed_row()])
    patch_transport(store, fake)
    dm_send.claim_dm_opportunity(store, OID, worker_id="w1")
    patch = fake.patches[-1]
    # the PATCH must be conditioned on status=eq.approved so two workers can't both win
    assert any(k.startswith("status") and "approved" in v for k, v in patch["params"].items())
    assert patch["body"].get("status") == "sending"


# ===========================================================================
# 3. ACCOUNT VERIFICATION
# ===========================================================================
def test_wrong_logged_in_account_fails_closed():
    from threads_operator import dm_send
    store, fake = make_store([seed_row()])
    patch_transport(store, fake)
    page = FakePage(logged_in_user="not_syaqir")
    res = dm_send.send_dm_opportunity(store, OID, page=page, worker_id="w1")
    assert res.ok is False
    assert res.failure_category == "account_mismatch"
    assert ("click_send",) not in [(c[0],) for c in page.calls]
    assert page.sent_messages == []


def test_account_cannot_be_determined_fails_closed():
    from threads_operator import dm_send
    store, fake = make_store([seed_row()])
    patch_transport(store, fake)
    page = FakePage(logged_in_user=None)
    res = dm_send.send_dm_opportunity(store, OID, page=page, worker_id="w1")
    assert res.ok is False
    assert res.failure_category == "account_mismatch"
    assert page.sent_messages == []


# ===========================================================================
# 4. AUTH / CHALLENGE fail closed
# ===========================================================================
def test_login_required_fails_closed():
    from threads_operator import dm_send
    store, fake = make_store([seed_row()])
    patch_transport(store, fake)
    page = FakePage(challenge="login")
    res = dm_send.send_dm_opportunity(store, OID, page=page, worker_id="w1")
    assert res.ok is False
    assert res.failure_category == "login_required"
    assert page.sent_messages == []


def test_captcha_stops_execution():
    from threads_operator import dm_send
    store, fake = make_store([seed_row()])
    patch_transport(store, fake)
    page = FakePage(challenge="captcha")
    res = dm_send.send_dm_opportunity(store, OID, page=page, worker_id="w1")
    assert res.ok is False
    assert res.failure_category in ("session_challenge", "captcha")
    assert page.sent_messages == []


# ===========================================================================
# 5. RECIPIENT RESOLUTION — exact canonical match only
# ===========================================================================
def test_exact_username_resolves():
    from threads_operator import dm_send
    store, fake = make_store([seed_row()])
    patch_transport(store, fake)
    page = FakePage(recipients=[TARGET])
    res = dm_send.send_dm_opportunity(store, OID, page=page, worker_id="w1")
    assert res.ok is True
    assert page.selected_recipient == TARGET


def test_partial_username_match_rejected():
    from threads_operator import dm_send
    store, fake = make_store([seed_row()])
    patch_transport(store, fake)
    page = FakePage(recipients=["hanisahnorazman_fan", "hanisah"])
    res = dm_send.send_dm_opportunity(store, OID, page=page, worker_id="w1")
    assert res.ok is False
    assert res.failure_category in ("recipient_not_found", "recipient_ambiguous")
    assert page.sent_messages == []


def test_ambiguous_results_rejected():
    from threads_operator import dm_send
    store, fake = make_store([seed_row()])
    patch_transport(store, fake)
    # two rows whose canonical username BOTH equal target (dup) -> ambiguous
    page = FakePage(recipients=[TARGET, TARGET])
    res = dm_send.send_dm_opportunity(store, OID, page=page, worker_id="w1")
    assert res.ok is False
    assert res.failure_category == "recipient_ambiguous"
    assert page.sent_messages == []


def test_recipient_not_found():
    from threads_operator import dm_send
    store, fake = make_store([seed_row()])
    patch_transport(store, fake)
    page = FakePage(recipients=["someone_else"])
    res = dm_send.send_dm_opportunity(store, OID, page=page, worker_id="w1")
    assert res.ok is False
    assert res.failure_category == "recipient_not_found"
    assert page.sent_messages == []


# ===========================================================================
# 6. EXACT TEXT + composer check
# ===========================================================================
def test_exact_approved_text_is_inserted():
    from threads_operator import dm_send
    store, fake = make_store([seed_row()])
    patch_transport(store, fake)
    page = FakePage()
    dm_send.send_dm_opportunity(store, OID, page=page, worker_id="w1")
    inserts = [c[1] for c in page.calls if c[0] == "insert"]
    assert inserts == [APPROVED_TEXT]


def test_composer_mismatch_aborts_before_send():
    from threads_operator import dm_send
    store, fake = make_store([seed_row()])
    patch_transport(store, fake)
    page = FakePage(composer_accept=False)  # composer text won't equal approved
    res = dm_send.send_dm_opportunity(store, OID, page=page, worker_id="w1")
    assert res.ok is False
    assert res.failure_category == "text_verification_failed"
    assert not any(c[0] == "click_send" for c in page.calls)
    assert page.sent_messages == []


# ===========================================================================
# 7. SEND CONFIRMATION + uncertain
# ===========================================================================
def test_positive_confirmation_transitions_to_sent():
    from threads_operator import dm_send
    store, fake = make_store([seed_row()])
    patch_transport(store, fake)
    page = FakePage(confirm_send=True)
    res = dm_send.send_dm_opportunity(store, OID, page=page, worker_id="w1")
    assert res.ok is True
    row = fake.rows[OID]
    assert row["status"] == "sent"
    assert row.get("sent_at")
    assert row.get("external_dm_id") == page.thread_id


def test_click_without_confirmation_goes_send_uncertain():
    from threads_operator import dm_send
    store, fake = make_store([seed_row()])
    patch_transport(store, fake)
    page = FakePage(confirm_send=False)  # click happened but confirmation unreadable
    res = dm_send.send_dm_opportunity(store, OID, page=page, worker_id="w1")
    assert res.ok is False
    assert res.failure_category == "send_uncertain"
    row = fake.rows[OID]
    assert row["status"] == "send_uncertain"
    # must NOT silently return to approved
    assert row["status"] != "approved"


def test_send_uncertain_does_not_auto_resend():
    from threads_operator import dm_send
    store, fake = make_store([seed_row(status="send_uncertain")])
    patch_transport(store, fake)
    page = FakePage()
    res = dm_send.send_dm_opportunity(store, OID, page=page, worker_id="w2")
    assert res.ok is False
    assert page.sent_messages == []


# ===========================================================================
# 8. RECONCILIATION
# ===========================================================================
def test_reconcile_finds_existing_message_marks_sent():
    from threads_operator import dm_send
    store, fake = make_store([seed_row(status="send_uncertain", claim_id="c1", attempt_count=1)])
    patch_transport(store, fake)
    page = FakePage()
    page.sent_messages = [APPROVED_TEXT]  # it actually went through
    res = dm_send.reconcile_dm_opportunity(store, OID, page=page)
    assert res.resolved is True
    assert fake.rows[OID]["status"] == "sent"


def test_reconcile_confidently_absent_allows_retry():
    from threads_operator import dm_send
    store, fake = make_store([seed_row(status="send_uncertain", claim_id="c1", attempt_count=1)])
    patch_transport(store, fake)
    page = FakePage()
    page.sent_messages = []  # confidently absent
    res = dm_send.reconcile_dm_opportunity(store, OID, page=page)
    assert res.resolved is True
    assert res.can_retry is True
    # back to approved for a controlled retry
    assert fake.rows[OID]["status"] == "approved"


def test_reconcile_still_uncertain_leaves_unresolved():
    from threads_operator import dm_send
    store, fake = make_store([seed_row(status="send_uncertain", claim_id="c1", attempt_count=1)])
    patch_transport(store, fake)
    page = FakePage(confirm_send=False)
    page.recent_outgoing_contains = lambda t: None  # cannot determine
    res = dm_send.reconcile_dm_opportunity(store, OID, page=page)
    assert res.resolved is False
    assert fake.rows[OID]["status"] == "send_uncertain"


# ===========================================================================
# 9. DUPLICATE PROTECTION
# ===========================================================================
def test_sent_opportunity_never_sends_again():
    from threads_operator import dm_send
    store, fake = make_store([seed_row(status="sent", sent_at="2026-09-25T03:00:00+00:00",
                                       external_dm_id="t1")])
    patch_transport(store, fake)
    page = FakePage()
    res = dm_send.send_dm_opportunity(store, OID, page=page, worker_id="w1")
    assert res.ok is False
    assert page.sent_messages == []


def test_platform_dm_restriction_clean_failure():
    from threads_operator import dm_send
    store, fake = make_store([seed_row()])
    patch_transport(store, fake)
    page = FakePage()
    page.open_conversation = lambda u: (_ for _ in ()).throw(
        dm_send.PlatformDMRestricted("recipient does not accept DMs"))
    res = dm_send.send_dm_opportunity(store, OID, page=page, worker_id="w1")
    assert res.ok is False
    assert res.failure_category == "platform_dm_restricted"
    assert page.sent_messages == []


# ===========================================================================
# 10. AUDIT / RESULT SHAPE
# ===========================================================================
def test_result_carries_audit_fields():
    from threads_operator import dm_send
    store, fake = make_store([seed_row()])
    patch_transport(store, fake)
    page = FakePage()
    res = dm_send.send_dm_opportunity(store, OID, page=page, worker_id="w1")
    assert res.opportunity_id == OID
    assert res.account == ACCOUNT
    assert res.target_username == TARGET
    assert res.attempt == 1
    assert res.confirmation_ref  # thread id / evidence ref
    assert isinstance(res.text_hash, str) and len(res.text_hash) >= 16
