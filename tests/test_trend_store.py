"""Tests for SupabaseStore trend-candidate ingress (mock transport, zero real writes)."""

import json

import httpx
import pytest

from threads_operator.supabase_store import SupabaseStore


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def make_store(handler, account_key="syaqir"):
    return SupabaseStore(
        "https://example.supabase.co",
        "secret",
        client=make_client(handler),
        account_key=account_key,
    )


URL = "https://www.threads.com/@someone/post/ABC123"


def test_insert_trend_candidate_posts_normalized_row_with_account_from_store():
    seen = {}

    def handler(request):
        if request.method == "GET":
            assert request.url.path == "/rest/v1/threads_trend_candidates"
            assert request.url.params["target_account_id"] == "eq.syaqir"
            assert request.url.params["source_permalink"] == f"eq.{URL}"
            return httpx.Response(200, json=[])
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=[{"id": 7}])

    store = make_store(handler)
    result = store.insert_trend_candidate(URL)
    assert seen["method"] == "POST"
    assert seen["path"] == "/rest/v1/threads_trend_candidates"
    body = seen["body"]
    assert body["target_account_id"] == "syaqir"
    assert body["source_permalink"] == URL
    assert body["source_platform"] == "threads"
    assert result["status"] == "inserted"
    assert result["permalink"] == URL


def test_insert_trend_candidate_never_fabricates_source_post_id():
    seen = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[])
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=[{"id": 1}])

    make_store(handler).insert_trend_candidate(URL)
    body = seen["body"]
    assert "source_post_id" not in body or body.get("source_post_id") is None


def test_optional_evidence_fields_are_stored():
    seen = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[])
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=[{"id": 1}])

    make_store(handler).insert_trend_candidate(
        "https://www.threads.com/@alice/post/XYZ",
        source_username="alice",
        source_text="hello",
        likes=10,
        views=100,
        published_at="2026-09-15T00:00:00+00:00",
    )
    body = seen["body"]
    assert body["source_username"] == "alice"
    assert body["source_text"] == "hello"
    assert body["likes"] == 10
    assert body["views"] == 100
    assert body["published_at"] == "2026-09-15T00:00:00+00:00"
    assert "replies" not in body or body.get("replies") is None


def test_raw_metadata_marks_manual_discovery_and_merges_extras():
    seen = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[])
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=[{"id": 1}])

    make_store(handler).insert_trend_candidate(
        URL, raw_metadata={"note": "from chatgpt ping"}
    )
    meta = seen["body"]["raw_metadata"]
    assert meta["manual"] is True
    assert meta["discovery_method"] == "manual_url"
    assert meta["note"] == "from chatgpt ping"


def test_analysis_fields_never_written_by_ingress():
    seen = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[])
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=[{"id": 1}])

    make_store(handler).insert_trend_candidate(URL)
    body = seen["body"]
    for analysis in (
        "topic",
        "content_category",
        "content_format",
        "hook_type",
        "tone",
        "keywords",
        "trend_score",
        "velocity_score",
        "why_it_works",
        "adaptation_angle",
    ):
        assert analysis not in body


def test_existing_candidate_returns_existing_without_post():
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200, json=[{"id": 42, "status": "discovered"}]
            )
        calls.append("UNEXPECTED-POST")
        return httpx.Response(201, json=[])

    result = make_store(handler).insert_trend_candidate(URL)
    assert calls == ["GET"]
    assert result["status"] == "existing"
    assert result["id"] == 42
    assert result["permalink"] == URL


def test_unique_conflict_on_race_degrades_to_existing():
    def handler(request):
        if request.method == "GET":
            # First dedup lookup misses; the re-check after the 409 finds the row.
            handler.gets = getattr(handler, "gets", 0) + 1
            if handler.gets == 1:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{"id": 5}])
        assert request.method == "POST"
        return httpx.Response(409, json={"code": "23505"})

    result = make_store(handler).insert_trend_candidate(URL)
    assert result["status"] == "existing"
    assert result["id"] == 5


def test_dedup_uses_normalized_permalink():
    seen = {}

    def handler(request):
        if request.method == "GET":
            seen["lookup"] = request.url.params["source_permalink"]
            return httpx.Response(200, json=[])
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=[{"id": 1}])

    make_store(handler).insert_trend_candidate(
        "https://threads.com/@Someone/post/ABC123?xid=Un2o#frag"
    )
    # Username lowercased (handles are case-insensitive), post code preserved
    # verbatim (codes are case-sensitive — never rewrite them).
    assert seen["lookup"] == (
        "eq.https://www.threads.com/@someone/post/ABC123"
    )
    assert seen["body"]["source_permalink"] == (
        "https://www.threads.com/@someone/post/ABC123"
    )


def test_cross_account_write_is_impossible():
    store = make_store(lambda request: httpx.Response(200, json=[]))
    with pytest.raises(TypeError):
        store.insert_trend_candidate(URL, target_account_id="victim")


def test_missing_account_key_refuses_trend_write():
    store = make_store(
        lambda request: pytest.fail("no request may be made"),
        account_key=None,
    )
    with pytest.raises(ValueError):
        store.insert_trend_candidate(URL)


def test_non_threads_url_rejected_before_any_request():
    store = make_store(
        lambda request: pytest.fail("no request may be made")
    )
    with pytest.raises(ValueError):
        store.insert_trend_candidate("https://evil.example/@a/post/B")
