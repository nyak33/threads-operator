"""Activity-attributed trend candidates: own_performance ingress (no cron).

Deterministic path: validated threads_activity_events row -> official Graph
API identity bridge (numeric fbid permalink -> canonical shortcode
permalink, exact id echo required) -> insert-or-existing trend candidate
via the existing store dedup identity (target_account_id,
source_permalink).

Hard rules:
- LLM never involved; browser never used here.
- A numeric permalink is ONLY ever stored after the API bridge proves the
  shortcode identity; unproven identity fails closed with no write.
- The Activity event's visible_username is the FOLLOWER, never the post
  author; it is stored solely as raw_metadata.activity_follower_username.
- Target account always comes from the store's own account_key; the event
  account_key must match it (no cross-account writes via crafted events).
- No status transitions, no automatic seeding, no scheduling here.
"""
from __future__ import annotations

from typing import Any

from .trend_urls import identity_forms, normalize_threads_post_url

__all__ = ["activity_candidate_metadata", "ingest_activity_candidate"]


def activity_candidate_metadata(event: dict[str, Any]) -> dict[str, Any]:
    """Factual attribution metadata for one Activity event.

    Only non-sensitive observed facts from the event row. No snippet text,
    no analysis. Follower username kept under an explicit
    activity_follower_username key (it is NOT the source author).
    """
    meta: dict[str, Any] = {}
    if event.get("id") is not None:
        meta["activity_event_id"] = event["id"]
    if event.get("match_method"):
        meta["activity_match_method"] = str(event["match_method"])
    if event.get("match_confidence"):
        meta["activity_match_confidence"] = str(event["match_confidence"])
    if event.get("collected_at"):
        meta["activity_collected_at"] = str(event["collected_at"])
    if event.get("visible_username"):
        meta["activity_follower_username"] = str(event["visible_username"])
    return meta


def ingest_activity_candidate(store, api, event: dict[str, Any]) -> dict[str, Any]:
    """Create/locate an own_performance candidate from one Activity event.

    Returns the insert_trend_candidate result dict
    ({"status": inserted|existing, "id", "permalink", ...} plus
    "numeric_id" when bridged). Raises ValueError — before ANY write — on:
    missing/unparseable matched_permalink, event account mismatch, or a
    failed/inconsistent API identity bridge.
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
    if not event_account:
        event_account = store_account

    permalink = event.get("matched_permalink")
    if not permalink or not str(permalink).strip():
        raise ValueError("activity event has no matched_permalink")
    forms = identity_forms(str(permalink))
    author_username = forms["username"]
    if not author_username:
        raise ValueError("matched_permalink missing @username")

    numeric_id: str | None = None
    canonical = str(permalink)
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
        canonical = b_canon
    else:
        canonical, _ = normalize_threads_post_url(permalink)

    metadata = activity_candidate_metadata(event)
    if numeric_id:
        metadata["activity_numeric_id"] = numeric_id

    result = store.insert_trend_candidate(
        canonical,
        candidate_role="own_performance",
        source_username=author_username,
        raw_metadata=metadata,
    )
    result = dict(result)
    if numeric_id:
        result["numeric_id"] = numeric_id
    return result
