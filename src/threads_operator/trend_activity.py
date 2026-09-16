"""Activity-attributed trend candidates: own_performance ingress (no cron).

Deterministic path: validated threads_activity_events row -> official Graph
API identity bridge (numeric fbid permalink -> canonical shortcode
permalink, exact id echo required) -> merge provenance into the ONE
candidate identified by (target_account_id, source_permalink).

Multi-source provenance (trend_provenance): an Activity event PROVES the
own_performance analytical role and adds activity_attribution as a
discovery method, without destroying how the row originally entered the
store (manual_url etc.). Roles/methods/attributions live in raw_metadata
lists; legacy scalar mirrors are kept for older readers. Activity
ingestion NEVER assigns external_trend (reserved for a future explicit
external-discovery workflow) and NEVER maps the follower username to the
post author.

Return statuses (deterministic, spec §10):
- inserted                      new candidate row created
- existing_unchanged            same event re-run; zero DB churn
- existing_provenance_updated   a (possibly different) event merged new
                                provenance into the existing row
- would_insert / would_update_provenance (with writes=0) under dry_run

Hard rules:
- LLM never involved; browser never used here.
- A numeric permalink is ONLY ever stored after the API bridge proves the
  shortcode identity; unproven identity fails closed with no write.
- Target account always comes from the store's own account_key; the event
  account_key must match it (no cross-account writes via crafted events).
- Provenance writes go through update_trend_candidate_provenance, which is
  account-scoped and raw_metadata-only: status, metrics, enrichment facts
  and analysis fields are unreachable from this path.
- No status transitions, no automatic seeding, no scheduling here.
"""
from __future__ import annotations

from typing import Any

from .trend_provenance import (
    activity_attribution_from_event,
    merge_activity_provenance,
    provenance_for_insert,
)
from .trend_urls import identity_forms, normalize_threads_post_url

__all__ = ["ingest_activity_candidate"]


def _bridge_to_canonical(api, event: dict[str, Any]) -> tuple[str, str, str | None]:
    """Validate event + permalink and return (canonical, author, numeric_id).

    Raises ValueError before ANY write on account mismatch, missing or
    unparseable permalink, or a failed/inconsistent identity bridge.
    """
    if not isinstance(event, dict):
        raise ValueError("activity event must be a dict")

    permalink = event.get("matched_permalink")
    if not permalink or not str(permalink).strip():
        raise ValueError("activity event has no matched_permalink")
    forms = identity_forms(str(permalink))
    author_username = forms["username"]
    if not author_username:
        raise ValueError("matched_permalink missing @username")

    numeric_id: str | None = None
    if forms["kind"] == "numeric":
        # Bridge required: numeric fbid -> proven shortcode permalink.
        numeric_id = forms["numeric_id"]
        identity = api.get_post_identity(numeric_id)
        if str(identity.get("id") or "") != numeric_id:
            raise ValueError("identity bridge echo mismatch")
        bridged = identity.get("permalink") or ""
        api_username = str(identity.get("username") or "").lower()
        if api_username and api_username != author_username:
            raise ValueError(
                "identity bridge username disagrees with permalink author")
        b_canon, _ = normalize_threads_post_url(bridged)
        b_forms = identity_forms(b_canon)
        if b_forms["kind"] != "shortcode":
            raise ValueError(
                "identity bridge returned a non-shortcode permalink")
        if b_forms["username"] != author_username:
            raise ValueError(
                "identity bridge permalink author disagrees with event URL")
        return b_canon, author_username, numeric_id
    canonical, _ = normalize_threads_post_url(permalink)
    return canonical, author_username, None


def ingest_activity_candidate(
    store,
    api,
    event: dict[str, Any],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Merge one Activity event's provenance into its canonical candidate.

    Raises ValueError — before ANY write — on: missing/unparseable
    matched_permalink, event account mismatch, or a failed/inconsistent
    API identity bridge.
    """
    if not isinstance(event, dict):
        raise ValueError("activity event must be a dict")
    event_account = str(event.get("account_key") or "").strip()
    store_account = getattr(store, "account_key", None)
    if not store_account:
        raise ValueError("store must carry account_key for candidate writes")
    if event_account and event_account != store_account:
        raise ValueError(
            "activity event account_key does not match store account")

    canonical, author_username, numeric_id = _bridge_to_canonical(api, event)
    attribution = activity_attribution_from_event(event, numeric_id=numeric_id)

    base = {"permalink": canonical}
    if numeric_id:
        base["numeric_id"] = numeric_id

    existing = store.find_trend_candidate_by_permalink(
        canonical, account_key=store_account)
    if existing is None:
        metadata = provenance_for_insert(attribution)
        if dry_run:
            return {
                **base,
                "status": "would_insert",
                "writes": 0,
                "preview_raw_metadata": metadata,
            }
        result = dict(store.insert_trend_candidate(
            canonical,
            candidate_role="own_performance",
            source_username=author_username,
            raw_metadata=metadata,
        ))
        if result.get("status") == "existing":
            # Lost a dedup race against an identical insert: merge into the
            # winner row instead of stopping at a bare "existing".
            winner = store.find_trend_candidate_by_permalink(
                canonical, account_key=store_account)
            if winner is not None:
                return _merge_existing(store, winner, attribution, base,
                                       insert_result=result)
            return {**base, **result}
        return {**base, **result}

    return _merge_existing(store, existing, attribution, base, dry_run=dry_run)


def _merge_existing(store, row, attribution, base, *, dry_run=False,
                    insert_result=None):
    candidate_id = row.get("id")
    if candidate_id is None:
        raise ValueError("existing candidate row has no id — refusing blind merge")
    merged, changed = merge_activity_provenance(
        row.get("raw_metadata") or {}, attribution)
    if not changed:
        out = {**base, "status": "existing_unchanged", "id": candidate_id,
               "writes": 0}
        if insert_result:
            out["insert"] = insert_result
        return out
    if dry_run:
        return {**base, "status": "would_update_provenance",
                "id": candidate_id, "writes": 0,
                "preview_raw_metadata": merged}
    updated = store.update_trend_candidate_provenance(
        candidate_id=candidate_id, raw_metadata=merged)
    if updated is None:
        # Row vanished or failed the account-scoped read: never blind-write.
        return {**base, "status": "existing_unchanged", "id": candidate_id,
                "writes": 0, "reason": "account-scoped re-read found no row"}
    out = {**base, "status": "existing_provenance_updated",
           "id": candidate_id, "writes": 1}
    if insert_result:
        out["insert"] = insert_result
    return out
