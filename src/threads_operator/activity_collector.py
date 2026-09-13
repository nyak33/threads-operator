"""Activity Follow Collector – reads Activity → Follows, stores events, matches posts.

This module is the glue layer: one function ``collect_activity_follows`` calls
the browser reader, parses nodes, inserts into Supabase, runs the deterministic
matcher against known own-posts, and stores the latest notification state.
Analytics are rebuilt from the raw event table through a SQL view. Failures stay
isolated from official Insights.
"""
from __future__ import annotations

import logging
import pathlib
from typing import Any

from .activity_browser import ActivityChallengeError
from .activity_parser import parse_activity_payload
from .activity_matcher import match_event as _match_single
from .text_normalizer import fallback_fingerprint
from .supabase_store import SupabaseStore

logger = logging.getLogger(__name__)


def collect_activity_follows(store: SupabaseStore,
                             profile_dir: str | pathlib.Path,
                             own_posts: list[dict[str, Any]] | None = None,
                             max_consecutive_retries: int = 1,
                             settle_seconds: float = 3.0,
                             persist: bool = True) -> dict:
    """One run of the Activity Follow collector."""
    retries = 0
    payload: dict | None = None

    while retries < max_consecutive_retries:
        try:
            result = store.read_activity_follows_sync(
                pathlib.Path(profile_dir), settle_seconds=settle_seconds)
            payload = result.get("payload")
            break
        except ActivityChallengeError as exc:
            logger.warning("Activity challenge detected — STOP")
            return {"success": False, "challenge_seen": True,
                    "error": str(exc)}
        except Exception as exc:
            retries += 1
            if retries >= max_consecutive_retries:
                logger.error("Activity read failed after %d attempts",
                             max_consecutive_retries, exc_info=exc)
                return {"success": False, "error": str(exc)}
            logger.info("Retry %d/%d on Activity read failure",
                        retries, max_consecutive_retries)

    if payload is None:
        return {"success": False, "error": "no payload retrieved"}

    events = parse_activity_payload(payload)
    if not events:
        return {"success": True, "new_events": 0, "matched": [],
                "skipped_duplicate": 0, "skipped_non_attribution": 0}

    posts = own_posts or store.list_posts()
    results: list[dict] = []
    for evt in events:
        m = _match_single(evt, posts)
        evt.update(m)
        from .text_normalizer import normalize_snippet
        evt["source_snippet_normalized"] = normalize_snippet(
            evt.get("source_snippet_raw") or "")
        evt["fallback_fingerprint"] = fallback_fingerprint(
            evt.get("notification_datetime"),
            evt.get("source_snippet_raw"),
            evt.get("visible_username"),
            evt.get("grouped_others_count"))
        if not evt.get("notification_id"):
            evt["notification_id"] = "fp:" + evt["fallback_fingerprint"]
        results.append(evt)

    if persist:
        store.insert_activity_events(results)
    else:
        for r in results:
            r["upserted"] = False

    new_events = [r for r in results if r.get("upserted")]
    dupes = [r for r in results if persist and not r.get("upserted")]

    def follows_for(confidence: str) -> int:
        return sum(int(r.get("follow_count") or 0) for r in results
                   if r.get("match_confidence") == confidence)

    high = follows_for("high")
    medium = follows_for("medium")
    low = follows_for("low")
    unknown = follows_for("unknown")
    total_follows = sum(int(r.get("follow_count") or 0) for r in results)

    logger.info(
        "Activity collection: %d rows / %d follows, %d new rows, %d dupes — "
        "%dH/%dM/%dL/%dU follows",
        len(results), total_follows, len(new_events), len(dupes),
        high, medium, low, unknown)
    return {"success": True, "total_events": len(results),
            "total_follows": total_follows,
            "new_events": len(new_events), "matched": [
                {"post_id": r["matched_post_id"],
                 "confidence": r["match_confidence"]}
                for r in results],
            "high": high, "medium": medium, "low": low,
            "unknown": unknown,
            "skipped_duplicate": len(dupes)}
