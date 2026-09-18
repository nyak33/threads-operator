"""RED tests: Activity-attributed own-performance trend candidates.

Covers (spec numbering):
 IDENTITY 9-13  numeric->canonical bridging via the official API ONLY with
                exact id echo; wrong/ambiguous identity fails closed.
 METADATA 14-18 own_performance role, activity_attribution discovery,
                manual=false, attribution facts preserved, follower
                username NEVER becomes source_username; external path
                remains external_trend/manual_url.
 ENRICHMENT 19 numeric candidate only exists after validated identity,
                so enrichment consumes the canonical shortcode permalink.
 ACCOUNT 25-26  event account_key must equal store account_key; target
                always from store.
No LLM, no browser, no cron: mock transport only.
"""

import json

import httpx
import pytest

from threads_operator.supabase_store import SupabaseStore, trend_candidate_payload
from threads_operator.threads_api import ThreadsAPI
from threads_operator.trend_activity import ingest_activity_candidate
from threads_operator.trend_urls import normalize_threads_post_url

NUM = "17909304312530892"
CODE = "Dc-CJfwk74-"
CANON_SHORTCODE = f"https://www.threads.com/@syaqir_sharani/post/{CODE}"
NUM_PERMALINK = f"https://www.threads.net/@syaqir_sharani/post/{NUM}"

EVENT = {
    "id": 6,
    "account_key": "syaqir",
    "matched_permalink": NUM_PERMALINK,
    "matched_post_id": NUM,
    "match_method": "normalized_snippet_unique",
    "match_confidence": "high",
    "visible_username": "zetatechmy",
    "collected_at": "2026-09-13T00:15:00+00:00",
    "notification_type": "follow",
}


class RecordingStore:
    """Captures insert/provenance calls; enforces account scoping."""

    def __init__(self, existing_permalink=None, existing_meta=None):
        self.account_key = "syaqir"
        self.calls = []
        self.provenance_calls = []
        self.existing_permalink = existing_permalink
        self.existing_meta = existing_meta or {}
        self._merged_meta = dict(self.existing_meta)

    def find_trend_candidate_by_permalink(self, permalink, *, account_key=None):
        assert account_key in (None, "syaqir")
        if self.existing_permalink and permalink == self.existing_permalink:
            return {"id": 99, "target_account_id": "syaqir",
                    "source_username": "syaqir_sharani",
                    "raw_metadata": dict(self._merged_meta)}
        return None

    def insert_trend_candidate(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        canon, _ = normalize_threads_post_url(url)
        if self.existing_permalink and canon == self.existing_permalink:
            return {"status": "existing", "id": 99, "permalink": canon}
        return {"status": "inserted", "id": 42, "permalink": canon}

    def update_trend_candidate_provenance(self, *, candidate_id, raw_metadata):
        self.provenance_calls.append(
            {"candidate_id": candidate_id,
             "raw_metadata": json.loads(json.dumps(raw_metadata))})
        self._merged_meta = dict(raw_metadata)
        return {"id": candidate_id}


def api_with(handler):
    return ThreadsAPI(
        "token-x",
        "user-x",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def graph_ok(request):
    """Serve the official-API permalink bridge for the numeric id."""
    tid = request.url.path.rstrip("/").split("/")[-1]
    if tid != NUM:
        return httpx.Response(404, json={"error": {"message": "no such media"}})
    return httpx.Response(200, json={
        "id": NUM,
        "permalink": CANON_SHORTCODE,
        "username": "syaqir_sharani",
    })


# ---------------------------------------------------------------- bridge
def test_numeric_candidate_ingested_via_validated_api_bridge():
    api = api_with(graph_ok)
    store = RecordingStore()
    result = ingest_activity_candidate(store, api, EVENT)
    assert result["status"] == "inserted"
    # stored permalink is the BRIDGED canonical shortcode URL, not numeric
    assert result["permalink"] == CANON_SHORTCODE
    call = store.calls[0]
    assert call["url"] == CANON_SHORTCODE
    assert call["candidate_role"] == "own_performance"


def test_wrong_numeric_id_fails_closed_no_insert():
    """API echo mismatch (or 404) => no candidate row is ever created."""
    def handler(request):
        return httpx.Response(200, json={"id": "999", "permalink": CANON_SHORTCODE})
    store = RecordingStore()
    with pytest.raises(ValueError):
        ingest_activity_candidate(store, api_with(handler), EVENT)
    assert store.calls == []


def test_bridge_missing_permalink_fails_closed():
    def handler(request):
        return httpx.Response(200, json={"id": NUM})
    store = RecordingStore()
    with pytest.raises(ValueError):
        ingest_activity_candidate(store, api_with(handler), EVENT)
    assert store.calls == []


def test_bridge_username_disagreement_fails_closed():
    def handler(request):
        return httpx.Response(200, json={
            "id": NUM,
            "permalink": "https://www.threads.com/@someone_else/post/" + CODE,
            "username": "someone_else"})
    store = RecordingStore()
    with pytest.raises(ValueError):
        ingest_activity_candidate(store, api_with(handler), EVENT)
    assert store.calls == []


def test_bridge_canonical_permalink_must_be_shortcode_form():
    """If the API ever echoes a numeric permalink, we refuse to store an
    unbridged identity (fail closed rather than enrich a numeric later)."""
    def handler(request):
        return httpx.Response(200, json={
            "id": NUM,
            "permalink": f"https://www.threads.com/@syaqir_sharani/post/{NUM}",
            "username": "syaqir_sharani"})
    store = RecordingStore()
    with pytest.raises(ValueError):
        ingest_activity_candidate(store, api_with(handler), EVENT)
    assert store.calls == []


# ---------------------------------------------------------------- dedup
def test_numeric_activity_url_merges_into_existing_shortcode_row():
    """The bridged permalink IS the manual-path identity => NO second row;
    the existing candidate gains Activity provenance instead (multi-source:
    one row, two discovery methods)."""
    api = api_with(graph_ok)
    store = RecordingStore(
        existing_permalink=CANON_SHORTCODE,
        existing_meta={"candidate_role": "external_trend", "manual": True,
                       "discovery_method": "manual_url"})
    result = ingest_activity_candidate(store, api, EVENT)
    assert result["status"] == "existing_provenance_updated"
    assert result["id"] == 99
    assert store.calls == []  # never a second insert attempt
    written = store.provenance_calls[0]["raw_metadata"]
    assert written["candidate_role"] == "external_trend"  # history preserved
    assert written["candidate_roles"] == ["own_performance"]
    assert written["discovery_methods"] == ["manual_url", "activity_attribution"]
    # idempotent second ingest: same event => unchanged, no churn
    again = ingest_activity_candidate(store, api, EVENT)
    assert again["status"] == "existing_unchanged"
    assert again["writes"] == 0


# -------------------------------------------------------------- metadata
def test_activity_metadata_block():
    """Assert on the actual Supabase POST body (real payload builder path)."""
    api = api_with(graph_ok)
    seen = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[])
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=[{"id": 42}])

    real_store = SupabaseStore(
        "https://example.supabase.co", "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        account_key="syaqir")
    result = ingest_activity_candidate(real_store, api, EVENT)
    assert result["status"] == "inserted"
    assert seen["body"]["source_permalink"] == CANON_SHORTCODE
    assert seen["body"]["source_username"] == "syaqir_sharani"
    meta = seen["body"]["raw_metadata"]
    assert meta["candidate_role"] == "own_performance"
    assert meta["discovery_method"] == "activity_attribution"
    assert meta["manual"] is False
    assert meta["activity_event_id"] == 6
    assert meta["activity_match_method"] == "normalized_snippet_unique"
    assert meta["activity_collected_at"] == "2026-09-13T00:15:00+00:00"
    assert meta["activity_numeric_id"] == NUM


def test_follower_username_never_mapped_to_source_username():
    api = api_with(graph_ok)
    store = RecordingStore()
    ingest_activity_candidate(store, api, EVENT)
    call = store.calls[0]
    assert call["source_username"] == "syaqir_sharani"   # post author
    assert call["raw_metadata"]["activity_follower_username"] == "zetatechmy"
    assert "visible_username" not in call["raw_metadata"]


def test_shortcode_activity_permalink_skips_api_bridge():
    """Already-canonical shortcode permalinks need no bridge."""
    store = RecordingStore()
    event = dict(EVENT, matched_permalink=CANON_SHORTCODE)

    def boom(request):  # pragma: no cover - must not be called
        raise AssertionError("no API call expected for shortcode permalink")

    result = ingest_activity_candidate(store, api_with(boom), event)
    assert result["status"] == "inserted"
    assert result["permalink"] == CANON_SHORTCODE


# ------------------------------------------------------------ validation
def test_event_without_matched_permalink_rejected():
    store = RecordingStore()
    with pytest.raises(ValueError):
        ingest_activity_candidate(store, api_with(graph_ok),
                                  dict(EVENT, matched_permalink=None))
    assert store.calls == []


def test_event_from_other_account_rejected():
    store = RecordingStore()
    with pytest.raises(ValueError):
        ingest_activity_candidate(store, api_with(graph_ok),
                                  dict(EVENT, account_key="other"))
    assert store.calls == []


def test_ambiguous_match_method_still_ingested_as_fact():
    """match_method is stored verbatim; filtering policy is the caller's."""
    api = api_with(graph_ok)
    store = RecordingStore()
    ingest_activity_candidate(store, api,
                              dict(EVENT, match_method="nearest_prior_inference"))
    assert store.calls[0]["raw_metadata"]["activity_match_method"] == \
        "nearest_prior_inference"


def test_manual_ingestion_default_is_role_free_channel_fact():
    """Default manual payload records the channel ONLY: manual=true /
    manual_url, candidate_role=manual_ingress — never external_trend."""
    payload = trend_candidate_payload("syaqir", CANON_SHORTCODE)
    meta = payload["raw_metadata"]
    assert meta["candidate_role"] == "manual_ingress"
    assert meta["manual"] is True
    assert meta["discovery_method"] == "manual_url"
    assert "external_trend" not in json.dumps(meta)


def test_own_performance_payload_via_insert_kwarg():
    payload = trend_candidate_payload(
        "syaqir", CANON_SHORTCODE, candidate_role="own_performance",
        raw_metadata={"activity_event_id": 3})
    meta = payload["raw_metadata"]
    assert meta["candidate_role"] == "own_performance"
    assert meta["manual"] is False
    assert meta["discovery_method"] == "activity_attribution"
    assert meta["activity_event_id"] == 3


def test_unknown_role_rejected():
    with pytest.raises(ValueError):
        trend_candidate_payload("syaqir", CANON_SHORTCODE, candidate_role="whatever")
