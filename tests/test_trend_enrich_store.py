"""RED tests: account-scoped, allowlisted factual UPDATE of a trend candidate.

v1 may update ONLY: source_post_id <- structured pk.
"""

import httpx
import pytest

from threads_operator.supabase_store import SupabaseStore

SHORTCODE = "DdGAw6kFfPA"
PERMALINK = f"https://www.threads.com/@syaqir_sharani/post/{SHORTCODE}"
SERVICE_KEY = "service-KEY-not-real"


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _store(captured):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200, json=[{"id": 1, "target_account_id": "syaqir", "status": "discovered"}]
        )

    captured = []
    store = SupabaseStore(
        "https://example.supabase.co",
        SERVICE_KEY,
        client=make_client(handler),
        account_key="syaqir",
    )
    return store, captured


def test_update_writes_only_source_post_id_account_scoped():
    store, captured = _store([])

    row = store.update_trend_candidate_source_post_id(
        candidate_id=1, source_post_id="1788000000000001"
    )

    assert row is not None
    assert row["id"] == 1
    req = captured[0]
    assert req.method == "PATCH"
    assert (
        req.url.path == "/rest/v1/threads_trend_candidates"
    ), req.url.path
    import json

    body = json.loads(req.content)
    assert body == {"source_post_id": "1788000000000001"}, body
    params = dict(req.url.params)
    assert params["id"] == "eq.1"
    assert params["target_account_id"] == "eq.syaqir"


def test_update_requires_account_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    store = SupabaseStore(
        "https://example.supabase.co",
        SERVICE_KEY,
        client=make_client(handler),
        account_key=None,
    )
    with pytest.raises(ValueError):
        store.update_trend_candidate_source_post_id(
            candidate_id=1, source_post_id="1788000000000001"
        )


def test_update_rejects_bad_id_and_nonnumeric_pk():
    store, captured = _store([])
    with pytest.raises(ValueError):
        store.update_trend_candidate_source_post_id(
            candidate_id=0, source_post_id="1788000000000001"
        )
    with pytest.raises(ValueError):
        store.update_trend_candidate_source_post_id(
            candidate_id=1, source_post_id="not-a-pk"
        )
    with pytest.raises(ValueError):
        store.update_trend_candidate_source_post_id(
            candidate_id=1, source_post_id=""
        )
    assert captured == []


def test_update_returns_none_when_no_row_matches():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    store = SupabaseStore(
        "https://example.supabase.co",
        SERVICE_KEY,
        client=make_client(handler),
        account_key="syaqir",
    )
    row = store.update_trend_candidate_source_post_id(
        candidate_id=99, source_post_id="1788000000000001"
    )
    assert row is None


def test_update_never_touches_status_or_analysis_fields():
    store, captured = _store([])
    store.update_trend_candidate_source_post_id(
        candidate_id=1, source_post_id="1788000000000001"
    )
    import json

    body = json.loads(captured[0].content)
    for forbidden in (
        "status",
        "views",
        "likes",
        "replies",
        "reposts",
        "quotes",
        "topic",
        "tone",
        "trend_score",
        "analysis",
        "raw_metadata",
        "source_permalink",
    ):
        assert forbidden not in body
