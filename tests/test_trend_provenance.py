"""Multi-source candidate provenance: merge helper, store path, ingest statuses.

Provenance representation (raw_metadata, no schema change):
  candidate_roles       : list of analytical roles (own_performance only when
                          proven by Activity; external_trend is NEVER added
                          automatically)
  discovery_methods     : list, e.g. ["manual_url", "activity_attribution"]
  activity_attributions : list of factual attribution dicts, deduped by
                          event_id

Legacy compatibility: the scalar candidate_role / discovery_method keys are
preserved untouched; a legacy scalar own_performance / activity_attribution
normalizes into the new lists when touched. Manual ingestion alone no longer
labels a row external_trend (role semantics: new manual rows use
manual_ingress).
"""
import json

import httpx
import pytest

from threads_operator.supabase_store import (
    SupabaseStore,
    trend_candidate_payload,
)
from threads_operator.trend_activity import ingest_activity_candidate
from threads_operator.trend_provenance import (
    candidate_roles,
    discovery_methods,
    merge_activity_provenance,
)

NUM = "17909304312530892"
CANON_SHORTCODE = "https://www.threads.com/@syaqir_sharani/post/Dc-CJfwk74-"
CANON_NUMERIC = f"https://www.threads.net/@syaqir_sharani/post/{NUM}"
POST_USERNAME = "syaqir_sharani"

ATTR4 = {
    "event_id": 4,
    "follower_username": "syed.sakinah",
    "match_method": "normalized_snippet_unique",
    "match_confidence": "high",
    "collected_at": "2026-09-15T04:30:00+00:00",
    "numeric_id": NUM,
}


# ------------------------------------------------------------------
# 1-12: provenance helper
# ------------------------------------------------------------------

def test_helper_adds_own_performance_role():
    meta, changed = merge_activity_provenance({}, ATTR4)
    assert meta["candidate_roles"] == ["own_performance"]
    assert changed is True


def test_helper_role_deduplicates():
    merged, _ = merge_activity_provenance({}, ATTR4)
    meta, changed = merge_activity_provenance(merged, ATTR4)
    assert meta["candidate_roles"] == ["own_performance"]
    assert changed is False


def test_helper_adds_discovery_method():
    meta, _ = merge_activity_provenance({}, ATTR4)
    assert meta["discovery_methods"] == ["activity_attribution"]


def test_helper_discovery_method_deduplicates():
    base = {"discovery_methods": ["manual_url", "activity_attribution"]}
    meta, _ = merge_activity_provenance(base, ATTR4)
    assert meta["discovery_methods"] == ["manual_url", "activity_attribution"]


def test_helper_adds_activity_attribution():
    meta, _ = merge_activity_provenance({}, ATTR4)
    assert meta["activity_attributions"] == [ATTR4]


def test_helper_same_event_id_deduplicates():
    base, _ = merge_activity_provenance({}, ATTR4)
    meta, changed = merge_activity_provenance(base, ATTR4)
    assert len(meta["activity_attributions"]) == 1
    assert changed is False


def test_helper_second_distinct_event_appends():
    base, _ = merge_activity_provenance({}, ATTR4)
    other = {**ATTR4, "event_id": 99, "follower_username": "another_fan"}
    meta, changed = merge_activity_provenance(base, other)
    assert [a["event_id"] for a in meta["activity_attributions"]] == [4, 99]
    assert changed is True


def test_helper_preserves_unrelated_raw_metadata():
    base = {"note": "keep me", "enrichment_source": "graphql:web_shortdoc"}
    meta, _ = merge_activity_provenance(base, ATTR4)
    assert meta["note"] == "keep me"
    assert meta["enrichment_source"] == "graphql:web_shortdoc"


def test_helper_legacy_scalar_role_and_method_preserved():
    base = {
        "candidate_role": "manual_ingress",
        "manual": True,
        "discovery_method": "manual_url",
    }
    meta, _ = merge_activity_provenance(base, ATTR4)
    assert meta["candidate_role"] == "manual_ingress"
    assert meta["manual"] is True
    assert meta["discovery_method"] == "manual_url"


def test_helper_legacy_own_performance_scalar_normalizes_into_roles():
    legacy = {
        "candidate_role": "own_performance",
        "manual": False,
        "discovery_method": "activity_attribution",
    }
    assert candidate_roles(legacy) == ["own_performance"]
    assert discovery_methods(legacy) == ["activity_attribution"]


def test_helper_legacy_activity_scalars_normalize_into_attributions():
    legacy = {
        "candidate_role": "own_performance",
        "discovery_method": "activity_attribution",
        "activity_event_id": 7,
        "activity_follower_username": "zetatechmy",
        "activity_match_method": "normalized_snippet_unique",
        "activity_match_confidence": "high",
        "activity_collected_at": "2026-09-15T04:30:01+00:00",
        "activity_numeric_id": NUM,
    }
    meta, changed = merge_activity_provenance(legacy, {**ATTR4, "event_id": 8})
    ids = [a["event_id"] for a in meta["activity_attributions"]]
    assert ids == [7, 8]  # legacy entry synthesized from scalars, not fabricated
    legacy_entry = meta["activity_attributions"][0]
    assert legacy_entry["follower_username"] == "zetatechmy"
    assert legacy_entry["numeric_id"] == NUM
    assert changed is True


def test_helper_never_promotes_legacy_external_trend_scalar():
    """A historical external_trend label (old manual schema) is NOT proof of
    an external-discovery role; it must not enter candidate_roles."""
    legacy = {"candidate_role": "external_trend", "manual": True,
              "discovery_method": "manual_url"}
    meta, _ = merge_activity_provenance(legacy, ATTR4)
    assert meta["candidate_roles"] == ["own_performance"]
    # untouched historical scalar stays as recorded fact
    assert meta["candidate_role"] == "external_trend"
    assert meta["manual"] is True


# ------------------------------------------------------------------
# 13-15: role semantics
# ------------------------------------------------------------------

def test_manual_ingestion_alone_does_not_assign_external_trend():
    payload = trend_candidate_payload("syaqir", CANON_SHORTCODE)
    meta = payload["raw_metadata"]
    assert meta["candidate_role"] == "manual_ingress"
    assert meta["manual"] is True
    assert meta["discovery_method"] == "manual_url"
    assert "external_trend" not in json.dumps(meta)
    assert "candidate_roles" not in meta  # no analytical role without discovery


def test_activity_provenance_implies_own_performance():
    meta, _ = merge_activity_provenance(
        {"manual": True, "discovery_method": "manual_url"}, ATTR4)
    assert "own_performance" in meta["candidate_roles"]


def test_activity_never_adds_external_trend_role():
    meta, _ = merge_activity_provenance({}, ATTR4)
    assert "external_trend" not in meta.get("candidate_roles", [])
    assert "external_trend" not in json.dumps(meta)


# ------------------------------------------------------------------
# 16-18: store provenance update
# ------------------------------------------------------------------

def make_store(handler):
    return SupabaseStore(
        "https://example.supabase.co",
        "secret-marker-never-printed",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        account_key="syaqir",
    )


def test_provenance_update_is_account_scoped():
    seen = []

    def handler(request):
        seen.append((request.method, json.loads(request.content)
                     if request.method == "PATCH" else None,
                     dict(request.url.params)))
        if request.method == "GET":
            return httpx.Response(200, json=[
                {"id": 7, "target_account_id": "syaqir", "raw_metadata": {}}])
        return httpx.Response(200, json=[
            {"id": 7, "target_account_id": "syaqir",
             "raw_metadata": {"candidate_roles": ["own_performance"]}}])

    store = make_store(handler)
    row = store.update_trend_candidate_provenance(
        candidate_id=7, raw_metadata={"candidate_roles": ["own_performance"]})
    assert row["id"] == 7
    patch = [s for s in seen if s[0] == "PATCH"]
    assert patch, "expected one PATCH"
    assert patch[0][2]["id"] == "eq.7"
    assert patch[0][2]["target_account_id"] == "eq.syaqir"


def test_provenance_update_foreign_account_cannot_write():
    seen = []

    def handler(request):
        seen.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=[])  # account filter excludes it
        return httpx.Response(200, json=[{"id": 7}])

    store = make_store(handler)
    assert store.update_trend_candidate_provenance(
        candidate_id=7, raw_metadata={"candidate_roles": ["own_performance"]}) is None
    assert seen == ["GET"]  # no PATCH ever issued


def test_provenance_update_changes_raw_metadata_only():
    seen = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[
                {"id": 7, "target_account_id": "syaqir", "raw_metadata": {}}])
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=[{"id": 7}])

    store = make_store(handler)
    store.update_trend_candidate_provenance(
        candidate_id=7, raw_metadata={"discovery_methods": ["manual_url"]})
    assert set(seen["body"].keys()) == {"raw_metadata"}


def test_provenance_update_rejects_bad_input():
    store = make_store(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(ValueError):
        store.update_trend_candidate_provenance(candidate_id=7, raw_metadata={})
    with pytest.raises(ValueError):
        store.update_trend_candidate_provenance(candidate_id=7, raw_metadata="nope")
    with pytest.raises(ValueError):
        store.update_trend_candidate_provenance(candidate_id=0, raw_metadata={"a": 1})


# ------------------------------------------------------------------
# 19-24: activity ingest statuses
# ------------------------------------------------------------------

class FakeStore:
    def __init__(self, dedup_row="missing", row_username=POST_USERNAME,
                 existing_meta=None):
        self.account_key = "syaqir"
        self.dedup_row = dedup_row
        self.row_username = row_username
        self.existing_meta = existing_meta or {}
        self.insert_records = []
        self.provenance_records = []  # one dict per provenance write

    def find_trend_candidate_by_permalink(self, permalink, *, account_key=None):
        assert account_key == "syaqir"
        if self.dedup_row == "missing":
            return None
        if self.dedup_row == "foreign":
            return {"id": 4, "target_account_id": "other",
                    "raw_metadata": self.existing_meta}
        return {"id": 1, "target_account_id": "syaqir",
                "source_username": self.row_username,
                "raw_metadata": self.existing_meta}

    def insert_trend_candidate(self, url, **call):
        call["url"] = url
        self.insert_records.append(call)
        return {"status": "inserted", "id": 11}

    def get_trend_candidate(self, candidate_id):
        if self.dedup_row != 1:
            return None
        return {"id": 1, "target_account_id": "syaqir",
                "raw_metadata": self.existing_meta}

    def update_trend_candidate_provenance(self, *, candidate_id, raw_metadata):
        self.provenance_records.append(
            {"candidate_id": candidate_id,
             "raw_metadata": json.loads(json.dumps(raw_metadata))})
        return {"id": candidate_id}


def event(**over):
    ev = {
        "id": 4,
        "account_key": "syaqir",
        "visible_username": "syed.sakinah",
        "matched_permalink": CANON_NUMERIC,
        "match_method": "normalized_snippet_unique",
        "match_confidence": "high",
        "collected_at": "2026-09-15T04:30:00+00:00",
    }
    ev.update(over)
    return ev


class FakeAPI:
    def get_post_identity(self, post_id):
        assert post_id == NUM
        return {"id": post_id, "permalink": CANON_SHORTCODE,
                "username": POST_USERNAME}


def test_ingest_new_activity_returns_inserted_with_provenance():
    store = FakeStore()
    result = ingest_activity_candidate(store, FakeAPI(), event())
    assert result["status"] == "inserted"
    meta = store.insert_records[0]["raw_metadata"]
    assert meta["candidate_roles"] == ["own_performance"]
    assert meta["discovery_methods"] == ["activity_attribution"]
    assert meta["activity_attributions"] == [ATTR4]


def test_ingest_existing_candidate_merges_provenance():
    store = FakeStore(
        dedup_row=1,
        existing_meta={"candidate_role": "manual_ingress", "manual": True,
                       "discovery_method": "manual_url"})
    result = ingest_activity_candidate(store, FakeAPI(), event())
    assert result["status"] == "existing_provenance_updated"
    assert store.insert_records == []
    written = store.provenance_records[0]["raw_metadata"]
    assert written["manual"] is True
    assert written["discovery_method"] == "manual_url"
    assert written["candidate_roles"] == ["own_performance"]
    assert written["discovery_methods"] == ["manual_url", "activity_attribution"]
    assert written["activity_attributions"] == [ATTR4]


def test_ingest_same_event_rerun_is_unchanged_no_db_churn():
    merged, _ = merge_activity_provenance({}, ATTR4)
    store = FakeStore(dedup_row=1, existing_meta=merged)
    result = ingest_activity_candidate(store, FakeAPI(), event())
    assert result["status"] == "existing_unchanged"
    assert store.provenance_records == []


def test_ingest_second_event_same_post_merges_no_new_row():
    merged, _ = merge_activity_provenance({}, ATTR4)
    store = FakeStore(dedup_row=1, existing_meta=merged)
    result = ingest_activity_candidate(
        store, FakeAPI(), event(id=99, visible_username="another_fan",
                               collected_at="2026-09-15T05:00:00+00:00"))
    assert result["status"] == "existing_provenance_updated"
    assert store.insert_records == []
    written = store.provenance_records[0]["raw_metadata"]
    assert [a["event_id"] for a in written["activity_attributions"]] == [4, 99]


def test_ingest_canonical_semantic_duplicate_merges_into_one_candidate():
    """Activity event whose URL is the numeric form of an already-manually-
    inserted shortcode candidate: one row, provenance merged, no duplicate."""
    store = FakeStore(
        dedup_row=1,
        existing_meta={"candidate_role": "external_trend", "manual": True,
                       "discovery_method": "manual_url"})
    result = ingest_activity_candidate(store, FakeAPI(), event())
    assert result["status"] == "existing_provenance_updated"
    assert store.insert_records == []
    written = store.provenance_records[0]["raw_metadata"]
    assert written["candidate_role"] == "external_trend"  # history untouched
    assert written["candidate_roles"] == ["own_performance"]


def test_ingest_never_overwrites_source_username_with_follower():
    store = FakeStore(
        dedup_row=1,
        existing_meta={"manual": True},
        row_username=POST_USERNAME)
    ingest_activity_candidate(store, FakeAPI(), event())
    assert store.insert_records == []
    for rec in store.provenance_records:
        assert "source_username" not in rec["raw_metadata"]
        assert rec["raw_metadata"]["activity_attributions"][0][
            "follower_username"] == "syed.sakinah"


# ------------------------------------------------------------------
# dry-run preview
# ------------------------------------------------------------------

def test_ingest_dry_run_existing_previews_without_writes():
    store = FakeStore(
        dedup_row=1,
        existing_meta={"candidate_role": "external_trend", "manual": True,
                       "discovery_method": "manual_url"})
    result = ingest_activity_candidate(store, FakeAPI(), event(), dry_run=True)
    assert store.provenance_records == []
    assert store.insert_records == []
    assert result["writes"] == 0
    preview = result["preview_raw_metadata"]
    assert preview["manual"] is True
    assert preview["discovery_method"] == "manual_url"
    assert "own_performance" in preview["candidate_roles"]
    assert preview["discovery_methods"] == ["manual_url", "activity_attribution"]
    assert preview["activity_attributions"] == [ATTR4]


def test_ingest_dry_run_new_previews_payload_without_insert():
    store = FakeStore()
    result = ingest_activity_candidate(store, FakeAPI(), event(), dry_run=True)
    assert store.insert_records == []
    assert result["writes"] == 0
    assert result["status"] == "would_insert"
    assert result["permalink"] == CANON_SHORTCODE


# ------------------------------------------------------------------
# 25: the live id=1 shape end-to-end through the helper
# ------------------------------------------------------------------

def test_live_id1_style_manual_metadata_merges_correctly():
    id1_meta = {
        "candidate_role": "external_trend",
        "manual": True,
        "discovery_method": "manual_url",
        "enrichment_source": "graphql:web_shortdoc",
        "views_source": "post_view_video_view",
    }
    merged, changed = merge_activity_provenance(id1_meta, ATTR4)
    assert changed is True
    assert merged["manual"] is True and merged["discovery_method"] == "manual_url"
    assert merged["candidate_roles"] == ["own_performance"]
    assert merged["discovery_methods"] == ["manual_url", "activity_attribution"]
    assert merged["activity_attributions"] == [ATTR4]
    assert merged["enrichment_source"] == "graphql:web_shortdoc"
    # idempotent: merging the result again changes nothing
    again, changed2 = merge_activity_provenance(merged, ATTR4)
    assert changed2 is False
    assert again == merged
