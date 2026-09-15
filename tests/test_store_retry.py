"""Supabase store GETs must self-heal from transient 502/503/504 gateway blips."""

from __future__ import annotations

import httpx
import pytest

from threads_operator.supabase_store import SupabaseStore, make_default_client


class FakeTransport(httpx.BaseTransport):
    """Canned statuses; optional JSON bodies indexed per successful call."""

    def __init__(self, statuses, json_bodies=None):
        self.statuses = list(statuses)
        self.json_bodies = json_bodies or []
        self.calls = 0
        self.ok_calls = 0

    def handle_request(self, request):
        status = self.statuses[min(self.calls, len(self.statuses) - 1)]
        self.calls += 1
        if status == 200:
            body = (
                self.json_bodies[self.ok_calls]
                if self.ok_calls < len(self.json_bodies)
                else "[]"
            )
            self.ok_calls += 1
            return httpx.Response(
                200,
                content=body.encode(),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(
            status,
            json={"message": "Gateway Timeout"},
            headers={"content-type": "application/json"},
        )


def _client(transport, sleeps):
    return make_default_client(inner=transport, sleep=sleeps.append, timeout=5)


def test_get_retries_until_200_with_ascending_backoff():
    sleeps: list[float] = []
    transport = FakeTransport([504, 502, 200])
    resp = _client(transport, sleeps).get("https://example.supabase.co/rest/v1/x")
    assert resp.status_code == 200
    assert transport.calls == 3
    assert sleeps and sleeps == sorted(sleeps)


def test_post_is_never_retried():
    transport = FakeTransport([504])
    client = _client(transport, [])
    with pytest.raises(httpx.HTTPStatusError):
        resp = client.post("https://example.supabase.co/rest/v1/x", json={})
        resp.raise_for_status()
    assert transport.calls == 1


def test_persistent_504_surfaces_after_attempts_exhausted():
    transport = FakeTransport([504] * 10)
    resp = _client(transport, []).get("https://example.supabase.co/rest/v1/x")
    assert resp.status_code == 504
    assert transport.calls == 4


def test_4xx_is_not_retried():
    transport = FakeTransport([404])
    resp = _client(transport, []).get("https://example.supabase.co/rest/v1/x")
    assert resp.status_code == 404
    assert transport.calls == 1


def test_store_survives_blip_end_to_end():
    sleeps: list[float] = []
    client = _client(
        FakeTransport([504, 200], json_bodies=['[{"a": 1}]']), sleeps
    )
    store = SupabaseStore("https://example.supabase.co", "key", client=client)
    row = store.latest_account_snapshot("acct-1")
    assert row == {"a": 1}
    assert len(sleeps) == 1
