import httpx

from threads_operator.engagement_queue import (
    approve_reply,
    claim_approved_reply,
    propose_reply,
)
from threads_operator.supabase_store import SupabaseStore


class FakeResponse:
    def __init__(self, rows=None, status_code=200):
        self._rows = rows if rows is not None else []
        self.status_code = status_code

    def json(self):
        return self._rows

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("boom", request=request, response=response)


class FakeClient:
    def __init__(self):
        self.calls = []
        self.get_rows = []
        self.post_rows = []
        self.patch_rows = []

    def get(self, url, headers=None, params=None):
        self.calls.append(("get", url, params, None))
        return FakeResponse(self.get_rows.pop(0) if self.get_rows else [])

    def post(self, url, headers=None, json=None):
        self.calls.append(("post", url, None, json))
        return FakeResponse(self.post_rows.pop(0) if self.post_rows else [])

    def patch(self, url, headers=None, params=None, json=None):
        self.calls.append(("patch", url, params, json))
        return FakeResponse(self.patch_rows.pop(0) if self.patch_rows else [])


def store(client):
    return SupabaseStore(
        "https://project.supabase.co",
        "service-key",
        client=client,
        account_key="syaqir",
    )


def test_propose_reply_is_pending_approval_and_account_scoped():
    client = FakeClient()
    client.get_rows = [[]]
    client.post_rows = [[{"id": 41, "status": "pending_approval"}]]

    result = propose_reply(
        store(client),
        source_post_id="1788000000000001",
        source_permalink="https://www.threads.com/@someone/post/ABC123",
        proposed_text="Betul juga ni. Kadang benda simple yang kita terlepas.",
        source_username="someone",
        score=88,
        reason="high relevance",
    )

    assert result["status"] == "pending_approval"
    post_call = next(call for call in client.calls if call[0] == "post")
    payload = post_call[3]
    assert payload["account_key"] == "syaqir"
    assert payload["action"] == "reply"
    assert payload["status"] == "pending_approval"
    assert payload["approval_channel"] == "telebot"
    assert payload["proposed_text"].startswith("Betul juga")


def test_propose_reply_dedupes_existing_action():
    client = FakeClient()
    client.get_rows = [[{"id": 9, "status": "pending_approval", "proposed_text": "old"}]]

    result = propose_reply(
        store(client),
        source_post_id="1788000000000001",
        source_permalink="https://www.threads.com/@someone/post/ABC123",
        proposed_text="new draft",
    )

    assert result == {
        "status": "pending_approval",
        "id": 9,
        "proposed_text": "old",
    }
    assert not any(call[0] == "post" for call in client.calls)


def test_approve_reply_only_transitions_pending_row_for_same_account():
    client = FakeClient()
    client.patch_rows = [[{"id": 7, "status": "approved"}]]

    row = approve_reply(store(client), 7, approval_ref="tg:message:123")

    assert row["status"] == "approved"
    patch = client.calls[-1]
    assert patch[2]["id"] == "eq.7"
    assert patch[2]["account_key"] == "eq.syaqir"
    assert patch[2]["status"] == "eq.pending_approval"
    assert patch[3]["status"] == "approved"
    assert patch[3]["approval_channel"] == "telebot"
    assert patch[3]["approval_ref"] == "tg:message:123"
    assert patch[3]["approved_at"]


def test_claim_requires_approved_state():
    client = FakeClient()
    client.patch_rows = [[{"id": 7, "status": "executing"}]]

    row = claim_approved_reply(store(client), 7)

    assert row["status"] == "executing"
    patch = client.calls[-1]
    assert patch[2]["status"] == "eq.approved"
    assert patch[3]["status"] == "executing"
