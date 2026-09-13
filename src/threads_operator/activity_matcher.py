"""Snippet → own-post matching with explicit confidence (no exactness lies).

Deterministic, pure-function matching used by the Activity Follow collector.
Candidates are our own known posts (thread_id, text, published_at,
permalink). Notification time is an UPPER BOUND only — a candidate published
after it is rejected outright.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .text_normalizer import normalize_snippet


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def eligible_candidates(event: dict[str, Any],
                        posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Own posts whose normalized text starts with the event's snippet."""
    event_norm = normalize_snippet(event.get("source_snippet_raw") or "")
    if not event_norm:
        return []
    notif_dt = _parse_dt(event.get("notification_datetime"))
    out = []
    for post in posts:
        published = _parse_dt(post.get("published_at"))
        if notif_dt is not None and published is not None and published > notif_dt:
            continue
        post_norm = normalize_snippet(post.get("text") or "")
        if not post_norm:
            continue
        if post_norm == event_norm or post_norm.startswith(event_norm):
            out.append(post)
    return out


def match_event(event: dict[str, Any],
                posts: list[dict[str, Any]]) -> dict[str, Any]:
    """Return deterministic match fields for one event dict."""
    candidates = eligible_candidates(event, posts)
    if not candidates:
        return {"matched_post_id": None, "matched_permalink": None,
                "match_method": "none", "match_confidence": "unknown"}
    if len(candidates) == 1:
        post = candidates[0]
        return {"matched_post_id": post.get("thread_id"),
                "matched_permalink": post.get("permalink"),
                "match_method": "normalized_snippet_unique",
                "match_confidence": "high"}
    if all(c.get("published_at") for c in candidates):
        _min = datetime.min.replace(tzinfo=timezone.utc)
        nearest = max(candidates,
                      key=lambda c: _parse_dt(c["published_at"]) or _min)
        distinct_times = len({c["published_at"] for c in candidates})
        if distinct_times == 1:
            return {"matched_post_id": None, "matched_permalink": None,
                    "match_method": "ambiguous_repeated_text",
                    "match_confidence": "low"}
        return {"matched_post_id": nearest.get("thread_id"),
                "matched_permalink": nearest.get("permalink"),
                "match_method": "nearest_prior_inference",
                "match_confidence": "medium"}
    return {"matched_post_id": None, "matched_permalink": None,
            "match_method": "ambiguous_repeated_text",
            "match_confidence": "low"}
