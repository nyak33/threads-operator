"""CLI tests for Task 2D DM send subcommands (deterministic; browser mocked)."""
import json

import pytest

from threads_operator import operator_cli

from tests.test_dm_send import make_store, seed_row, FakePage, APPROVED_TEXT, OID, TARGET


class _Cfg:
    def __init__(self):
        self.name = "syaqir"
        self._v = {
            "SUPABASE_URL": "https://supabase.test",
            "SUPABASE_SERVICE_ROLE_KEY": "service-KEY-not-real",
            "THREADS_BROWSER_PROFILE": "/nonexistent",
        }

    def require(self, k):
        return self._v[k]

    def get(self, k, default=None):
        return self._v.get(k, default)


def _patch_store_and_page(monkeypatch, store, page):
    monkeypatch.setattr(operator_cli, "_store", lambda cfg: store)
    monkeypatch.setattr(operator_cli, "_dm_page", lambda cfg: page)
    # ThreadsDMPage.close() on the fake page is a no-op
    page.close = lambda: None


def test_cli_send_approved(monkeypatch):
    store, fake = make_store([seed_row()])
    page = FakePage()
    _patch_store_and_page(monkeypatch, store, page)
    code, payload = operator_cli._run_dm_send(_Cfg(), opportunity_id=OID, worker_id="w1")
    assert code == 0
    assert payload["ok"] is True
    assert payload["status"] == "sent"
    assert payload["confirmation_ref"]
    assert fake.rows[OID]["status"] == "sent"


def test_cli_send_not_approved_fails_closed(monkeypatch):
    store, fake = make_store([seed_row(status="awaiting_approval")])
    page = FakePage()
    _patch_store_and_page(monkeypatch, store, page)
    code, payload = operator_cli._run_dm_send(_Cfg(), opportunity_id=OID, worker_id="w1")
    assert code != 0
    assert payload["ok"] is False
    assert page.sent_messages == []


def test_cli_send_never_sends_sent_row(monkeypatch):
    store, fake = make_store([seed_row(status="sent", sent_at="2026-09-25T03:00:00+00:00")])
    page = FakePage()
    _patch_store_and_page(monkeypatch, store, page)
    code, payload = operator_cli._run_dm_send(_Cfg(), opportunity_id=OID, worker_id="w1")
    assert code != 0
    assert page.sent_messages == []


def test_cli_send_next_empty(monkeypatch):
    store, fake = make_store([])
    _patch_store_and_page(monkeypatch, store, FakePage())
    code, payload = operator_cli._run_dm_send_next(_Cfg(), worker_id="w1")
    assert code == 0
    assert payload["sent"] == 0


def test_cli_send_queue_groups(monkeypatch):
    store, fake = make_store([
        seed_row(id=1, status="approved"),
        seed_row(id=2, status="sending", claim_id="c2", attempt_count=1),
        seed_row(id=3, status="send_uncertain", claim_id="c3", attempt_count=1),
    ])
    _patch_store_and_page(monkeypatch, store, FakePage())
    code, payload = operator_cli._run_dm_send_queue(_Cfg(), limit=50)
    assert code == 0
    assert [r["id"] for r in payload["approved"]] == [1]
    assert [r["id"] for r in payload["sending"]] == [2]
    assert [r["id"] for r in payload["send_uncertain"]] == [3]


def test_cli_inspect(monkeypatch):
    store, fake = make_store([seed_row(status="send_uncertain", claim_id="c9")])
    _patch_store_and_page(monkeypatch, store, FakePage())
    code, payload = operator_cli._run_dm_inspect(_Cfg(), opportunity_id=OID)
    assert code == 0
    opp = payload["opportunity"]
    assert opp["status"] == "send_uncertain"
    assert opp["claim_id"] == "c9"


def test_cli_reconcile_marks_sent(monkeypatch):
    store, fake = make_store([seed_row(status="send_uncertain", claim_id="c1", attempt_count=1)])
    page = FakePage()
    page.sent_messages = [APPROVED_TEXT]
    _patch_store_and_page(monkeypatch, store, page)
    code, payload = operator_cli._run_dm_reconcile(_Cfg(), opportunity_id=OID)
    assert code == 0
    assert payload["outcome"] == "sent"
    assert fake.rows[OID]["status"] == "sent"


def test_cli_reconcile_absent_allows_retry(monkeypatch):
    store, fake = make_store([seed_row(status="send_uncertain", claim_id="c1", attempt_count=1)])
    page = FakePage()
    page.sent_messages = []
    _patch_store_and_page(monkeypatch, store, page)
    code, payload = operator_cli._run_dm_reconcile(_Cfg(), opportunity_id=OID)
    assert code == 0
    assert payload["can_retry"] is True
    assert fake.rows[OID]["status"] == "approved"
