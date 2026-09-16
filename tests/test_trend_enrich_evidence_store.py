"""RED tests (v2): store-layer update_trend_candidate_evidence.

Spec section 8: the store method itself must enforce
  WHERE id = candidate_id AND target_account_id = account_key
and a strict evidence-field allowlist. Unknown/prohibited keys (status,
views, topic, trend_score, analysis fields, ...) are rejected by the
method BEFORE any HTTP call. Absent evidence fields are never nulled.
raw_metadata is merged over a fresh account-scoped read, never replaced.
A foreign-account candidate behaves as not found and is never modified.
"""

import json

import httpx
import pytest

from threads_operator.supabase_store import SupabaseStore

SERVICE_KEY = "service-KEY-not-real"


def make_store(handler):
    return SupabaseStore(
        "https://example.supabase.co",
        SERVICE_KEY,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        account_key="syaqir",
    )


def _route(handler_requests, *, existing_raw=None, row_found=True):
    """GET -> account-scoped row (for raw_metadata merge), PATCH -> result."""

    def handler(request: httpx.Request) -> httpx.Response:
        handler_requests.append(request)
        if request.method == "GET":
            if not row_found:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "target_account_id": "syaqir",
                        "raw_metadata": existing_raw,
                    }
                ],
            )
        return httpx.Response(
            200,
            json=[{"id": 1, "target_account_id": "syaqir", "status": "discovered"}],
        )

    return handler


GOOD_EVIDENCE = {
    "source_post_id": "1788000000000001",
    "source_username": "syaqir_sharani",
    "source_text": "kita ni belakang kira",
    "published_at": "2025-09-08T12:06:40+00:00",
    "likes": 9,
    "replies": 1,
    "reposts": 1,
    "quotes": 0,
    "last_checked_at": "2026-09-16T08:00:00+00:00",
}


# ------------------------------------------------------------- happy path

def test_evidence_update_patches_allowlisted_fields_account_scoped():
    reqs = []
    store = make_store(_route(reqs, existing_raw={"manual": True}))

    row = store.update_trend_candidate_evidence(
        candidate_id=1, evidence=dict(GOOD_EVIDENCE)
    )
    assert row is not None and row["id"] == 1

    patches = [r for r in reqs if r.method == "PATCH"]
    assert len(patches) == 1
    req = patches[0]
    assert req.url.path == "/rest/v1/threads_trend_candidates"
    params = dict(req.url.params)
    assert params["id"] == "eq.1"
    assert params["target_account_id"] == "eq.syaqir"
    body = json.loads(req.content)
    for key, value in GOOD_EVIDENCE.items():
        assert body[key] == value


def test_absent_evidence_fields_are_omitted_not_nulled():
    reqs = []
    store = make_store(_route(reqs, existing_raw=None))

    store.update_trend_candidate_evidence(
        candidate_id=1, evidence={"source_post_id": "1788000000000001", "likes": 0}
    )
    body = json.loads([r for r in reqs if r.method == "PATCH"][0].content)
    assert body == {"source_post_id": "1788000000000001", "likes": 0}
    for missing in ("source_text", "published_at", "replies", "reposts", "quotes"):
        assert missing not in body
        assert missing not in body or body.get(missing) is not None
    assert "null" not in json.dumps(body)


def test_raw_metadata_is_merged_not_replaced():
    reqs = []
    store = make_store(
        _route(reqs, existing_raw={"manual": True, "discovery_method": "manual_url"})
    )

    store.update_trend_candidate_evidence(
        candidate_id=1,
        evidence={"source_post_id": "1788000000000001"},
        enrichment={"enrichment_source": "permalink_preloader", "media_type": 19},
    )
    body = json.loads([r for r in reqs if r.method == "PATCH"][0].content)
    merged = body["raw_metadata"]
    assert merged["manual"] is True
    assert merged["discovery_method"] == "manual_url"
    assert merged["enrichment_source"] == "permalink_preloader"
    assert merged["media_type"] == 19


def test_existing_raw_metadata_dict_is_read_back_via_account_scoped_get():
    reqs = []
    store = make_store(_route(reqs, existing_raw={"manual": True}))
    store.update_trend_candidate_evidence(
        candidate_id=1,
        evidence={"source_post_id": "1788000000000001"},
        enrichment={"enrichment_source": "permalink_preloader"},
    )
    gets = [r for r in reqs if r.method == "GET"]
    assert len(gets) == 1
    params = dict(gets[0].url.params)
    assert params["id"] == "eq.1"
    assert params["target_account_id"] == "eq.syaqir"


def test_no_enrichment_means_no_raw_metadata_in_patch():
    reqs = []
    store = make_store(_route(reqs))
    store.update_trend_candidate_evidence(
        candidate_id=1, evidence={"likes": 3}
    )
    body = json.loads([r for r in reqs if r.method == "PATCH"][0].content)
    assert "raw_metadata" not in body
    # no merge read needed either
    assert [r for r in reqs if r.method == "GET"] == []


# ------------------------------------------------------------- allowlist

@pytest.mark.parametrize(
    "bad_key",
    [
        "status",
        "views",
        "topic",
        "content_category",
        "content_format",
        "hook_type",
        "tone",
        "keywords",
        "velocity_score",
        "trend_score",
        "why_it_works",
        "adaptation_angle",
        "used_in_queue_id",
        "raw_metadata",
        "target_account_id",
        "id",
        "source_permalink",
        "source_platform",
        "discovered_at",
    ],
)
def test_prohibited_evidence_keys_rejected_before_any_http(bad_key):
    reqs = []
    store = make_store(_route(reqs))
    evidence = {"source_post_id": "1788000000000001", bad_key: "whatever"}
    with pytest.raises(ValueError):
        store.update_trend_candidate_evidence(candidate_id=1, evidence=evidence)
    assert reqs == []


def test_empty_evidence_rejected():
    reqs = []
    store = make_store(_route(reqs))
    with pytest.raises(ValueError):
        store.update_trend_candidate_evidence(candidate_id=1, evidence={})
    assert reqs == []


# ------------------------------------------------------------- value checks

@pytest.mark.parametrize(
    "evidence",
    [
        {"source_post_id": "not-a-pk"},
        {"source_post_id": ""},
        {"likes": -5},
        {"likes": True},
        {"quotes": "many"},
        {"published_at": "2025-09-08T12:06:40"},  # naive, no tz
        {"published_at": "yesterday"},
        {"last_checked_at": "nope"},
        {"source_username": "   "},
        {"source_username": "has space"},
        {"source_text": ""},
    ],
)
def test_malformed_evidence_values_fail_closed(evidence):
    reqs = []
    store = make_store(_route(reqs))
    with pytest.raises(ValueError):
        store.update_trend_candidate_evidence(candidate_id=1, evidence=evidence)
    assert reqs == []


def test_zero_counts_pass_validation():
    reqs = []
    store = make_store(_route(reqs))
    store.update_trend_candidate_evidence(
        candidate_id=1, evidence={"likes": 0, "replies": 0, "reposts": 0, "quotes": 0}
    )
    assert len([r for r in reqs if r.method == "PATCH"]) == 1


# ------------------------------------------------------------- scoping

def test_foreign_account_row_reads_as_not_found_no_patch():
    reqs = []

    def handler(request: httpx.Request) -> httpx.Response:
        reqs.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[])  # account-scoped filter: no row
        return httpx.Response(200, json=[{"id": 1, "target_account_id": "syaqir"}])

    store = make_store(handler)
    row = store.update_trend_candidate_evidence(
        candidate_id=99,
        evidence={"source_post_id": "1788000000000001"},
        enrichment={"enrichment_source": "permalink_preloader"},
    )
    assert row is None
    assert [r for r in reqs if r.method == "PATCH"] == []


def test_corrupt_existing_raw_metadata_fails_closed():
    """A non-dict raw_metadata must not be silently destroyed."""
    reqs = []
    store = make_store(_route(reqs, existing_raw="a string, not an object"))
    with pytest.raises(ValueError):
        store.update_trend_candidate_evidence(
            candidate_id=1,
            evidence={"source_post_id": "1788000000000001"},
            enrichment={"enrichment_source": "permalink_preloader"},
        )
    assert [r for r in reqs if r.method == "PATCH"] == []


def test_requires_account_key_and_valid_id():
    reqs = []
    store = SupabaseStore(
        "https://example.supabase.co",
        SERVICE_KEY,
        client=httpx.Client(transport=httpx.MockTransport(_route(reqs))),
        account_key=None,
    )
    with pytest.raises(ValueError):
        store.update_trend_candidate_evidence(
            candidate_id=1, evidence={"likes": 1}
        )
    assert reqs == []

    store2 = make_store(_route(reqs))
    with pytest.raises(ValueError):
        store2.update_trend_candidate_evidence(candidate_id=0, evidence={"likes": 1})
    with pytest.raises(ValueError):
        store2.update_trend_candidate_evidence(
            candidate_id=True, evidence={"likes": 1}
        )
    assert reqs == []
