"""Deterministic, fail-closed extractor for Threads trend-candidate enrichment.

Input: the authenticated navigation-document text + the expected permalink
shortcode. Output: structured factual evidence {code, pk}.

Primary source is the Relay ``__bbox`` preloader JSON embedded in the
document (target post under structures like data.feedData.edges[] ->
node.text_post_app_thread -> thread_items[] -> post/media object). No visual
DOM selectors, no OG-metadata fallback, no guessing: if the exact shortcode
is not found in parseable structured payload with a pk, we fail closed.

Challenge detection reuses the same marker list as the Activity reader so a
login/security page raises a distinct error class before parsing.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .activity_browser import _CHALLENGE_MARKERS
from .trend_urls import normalize_threads_post_url

__all__ = [
    "TrendEnrichError",
    "TrendChallengeError",
    "expected_shortcode",
    "extract_post_evidence",
]

# Built like activity_browser._PRELOADER_KEY: exact marker, no source-literal
# collision with test fixtures.
_BBOX_KEY = "__" + "bbox" + '"'


class TrendEnrichError(RuntimeError):
    """Fail-closed: no trustworthy structured evidence for the shortcode."""


class TrendChallengeError(TrendEnrichError):
    """Login/checkpoint page detected — STOP, do not bypass."""


def expected_shortcode(permalink: str) -> str:
    """Extract the post code verbatim from a canonical Threads permalink.

    Delegates all URL validation to trend_urls.normalize_threads_post_url
    (https-only, exact threads.com hosts, no userinfo/port, code preserved
    verbatim), then reads the code back off the canonical URL. Raises
    ValueError for anything that is not a Threads post permalink.
    """
    canonical, _username = normalize_threads_post_url(permalink)
    return canonical.rsplit("/post/", 1)[1]


def _parse_result_object(document_text: str, key_idx: int, next_key_idx: int) -> Any:
    """Parse the preloader "result" value (same intent as
    activity_browser._parse_result_object) using raw_decode, which balances
    braces/strings correctly regardless of where the block window ends."""
    window = (
        document_text[key_idx:next_key_idx]
        if next_key_idx != -1
        else document_text[key_idx:]
    )
    result_pos = window.find('"result"')
    if result_pos == -1:
        return None
    colon = window.find(":", result_pos + len('"result"'))
    if colon == -1:
        return None
    start = colon + 1
    while start < len(window) and window[start] in " \t\r\n":
        start += 1
    if start >= len(window) or window[start] not in "{[":
        return None
    try:
        value, _end = json.JSONDecoder().raw_decode(window, start)
    except json.JSONDecodeError as exc:
        raise TrendEnrichError(f"preloader JSON unparsable: {exc}") from exc
    return value


def _walk_posts(value: Any):
    """Yield every dict in the structure that looks like a post object
    (has 'code' and a pk-ish id). Recursive, generator-based."""
    if isinstance(value, dict):
        code = value.get("code")
        pk = value.get("pk") or value.get("id")
        if isinstance(code, str) and code and pk is not None:
            yield {"code": code, "pk": str(pk)}
        for child in value.values():
            yield from _walk_posts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_posts(child)


def _looks_like_challenge(text: str) -> bool:
    head = text[:60000].lower()
    if not any(marker in head for marker in _CHALLENGE_MARKERS):
        return False
    # A genuine post document can mention e.g. "login" in prose; only treat
    # it as a challenge when NO structured preloader is present at all.
    return _BBOX_KEY not in text


def extract_post_evidence(document_text: str, shortcode: str) -> dict[str, str]:
    """Return {"code", "pk"} for the post whose code == shortcode.

    Fail-closed (TrendEnrichError) when: document empty, shortcode absent or
    blank, only OG metadata available, preloader unparsable, no structured
    match, conflicting pk values for the same code, or a match without pk.
    """
    if not isinstance(shortcode, str) or not shortcode.strip():
        raise ValueError("expected shortcode must be a non-blank string")
    if not document_text or not document_text.strip():
        raise TrendEnrichError("empty navigation document")
    if _looks_like_challenge(document_text):
        raise TrendChallengeError("login/security checkpoint page — STOP")

    matches: set[tuple[str, str]] = set()
    found_key = False
    search_from = 0
    while True:
        idx = document_text.find(_BBOX_KEY, search_from)
        if idx == -1:
            break
        found_key = True
        next_idx = document_text.find(_BBOX_KEY, idx + len(_BBOX_KEY))
        payload = _parse_result_object(
            document_text, idx, next_idx if next_idx != -1 else len(document_text)
        )
        if payload is not None:
            for post in _walk_posts(payload):
                if post["code"] == shortcode:
                    matches.add((post["code"], post["pk"]))
        if next_idx == -1:
            break
        search_from = next_idx

    if not found_key:
        raise TrendEnrichError(
            "no Relay preloader payload in document (OG-only or schema drift) "
            "— fail closed, no anonymous fallback in v1"
        )
    if not matches:
        raise TrendEnrichError(
            f"structured preloader present but no post with code {shortcode!r}"
        )
    pks = {pk for (_code, pk) in matches}
    if len(pks) > 1:
        raise TrendEnrichError(
            f"conflicting structured matches for {shortcode!r}: pks {sorted(pks)}"
        )
    return {"code": shortcode, "pk": pks.pop()}
