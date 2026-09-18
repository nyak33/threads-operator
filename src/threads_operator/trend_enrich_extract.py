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
    "extract_post_facts",
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


def _walk_with_parents(value: Any, parent: Any = None):
    """Yield (dict_node, parent_dict_or_None) for every dict in the tree."""
    if isinstance(value, dict):
        yield value, parent
        for child in value.values():
            yield from _walk_with_parents(child, value)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_with_parents(child, parent)


def _bbox_payloads(document_text: str):
    """Yield each parseable Relay preloader result object in the document.

    Raises TrendEnrichError on malformed JSON (fail closed — never parse a
    truncated payload into partial facts).
    """
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
            yield payload
        if next_idx == -1:
            break
        search_from = next_idx
    if not found_key:
        raise TrendEnrichError(
            "no Relay preloader payload in document (OG-only or schema drift) "
            "— fail closed, no anonymous fallback"
        )


def _clean_count(node: dict, key: str) -> int | None:
    """Non-negative int at node[key] if genuinely present; bool is not int."""
    if key not in node:
        return None
    value = node[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TrendEnrichError(f"{key} is not a valid non-negative count: {value!r}")
    return value


def _utc_from_taken_at(value: Any) -> str:
    import datetime as _dt

    if isinstance(value, bool):
        raise TrendEnrichError("taken_at must be an integer epoch")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    if not isinstance(value, int) or value < 0:
        raise TrendEnrichError(f"taken_at is not a valid epoch: {value!r}")
    now = _dt.datetime.now(_dt.timezone.utc).timestamp()
    if value > now + 86400 or value > 4102444800:  # > 2100: schema drift
        raise TrendEnrichError(f"taken_at is implausibly far in the future: {value!r}")
    return _dt.datetime.fromtimestamp(value, _dt.timezone.utc).isoformat()


def _facts_from_node(node: dict, parent_lookup) -> dict[str, Any]:
    """Build an OBSERVED-ONLY fact dict from one matched post node.

    Absent/blank fields are omitted entirely (never null) so a later payload
    that omits a field cannot erase previously known facts. view_count and
    every token-ish key are structurally unreachable from this mapping.
    """
    pk = node.get("pk") or node.get("id")
    if pk is None:
        raise TrendEnrichError("matched post node without pk")
    facts: dict[str, Any] = {"source_post_id": str(pk)}

    user = node.get("user")
    if isinstance(user, dict):
        username = user.get("username")
        if isinstance(username, str) and username.strip():
            facts["source_username"] = username.strip()

    text = node.get("text")
    if not isinstance(text, str) or not text.strip():
        caption = node.get("caption")
        if isinstance(caption, dict):
            text = caption.get("text")
    if isinstance(text, str) and text.strip():
        facts["source_text"] = text

    if node.get("taken_at") is not None:
        facts["published_at"] = _utc_from_taken_at(node["taken_at"])

    likes = _clean_count(node, "like_count")
    if likes is not None:
        facts["likes"] = likes

    app_info = node.get("text_post_app_info")
    app_info = app_info if isinstance(app_info, dict) else {}
    replies = _clean_count(app_info, "direct_reply_count")
    if replies is None:
        replies = _clean_count(app_info, "reply_count")
    if replies is None:
        replies = _clean_count(node, "direct_reply_count")
    if replies is None:
        replies = _clean_count(node, "reply_count")
    if replies is not None:
        facts["replies"] = replies
    reposts = _clean_count(app_info, "repost_count")
    if reposts is None:
        reposts = _clean_count(node, "repost_count")
    if reposts is not None:
        facts["reposts"] = reposts
    quotes = _clean_count(app_info, "quote_count")
    if quotes is None:
        quotes = _clean_count(node, "quote_count")
    if quotes is not None:
        facts["quotes"] = quotes

    media_type = node.get("media_type")
    if isinstance(media_type, int) and not isinstance(media_type, bool):
        facts["media_type"] = media_type

    # self_thread_length: the thread container that owns this post node.
    holder = parent_lookup.get(id(node))
    if holder is not None:
        container = parent_lookup.get(id(holder))
        items = container.get("thread_items") if isinstance(container, dict) else None
        if isinstance(items, list) and any(
            isinstance(i, dict) and i.get("post") is node for i in items
        ):
            facts["self_thread_length"] = len(items)

    return facts


def extract_post_facts(document_text: str, shortcode: str) -> dict[str, Any]:
    """Return ONLY observed factual evidence for the post matching shortcode.

    Same fail-closed identity contract as extract_post_evidence (exact code
    match, conflicting pk = error, no pk = error, challenge = error), plus
    validated counters (non-negative ints; present-as-0 kept), UTC
    published_at from taken_at, username, text (post text else caption),
    media_type, and self_thread_length. Unknown fields — especially any
    view-ish key — are never mapped.
    """
    if not isinstance(shortcode, str) or not shortcode.strip():
        raise ValueError("expected shortcode must be a non-blank string")
    if not document_text or not document_text.strip():
        raise TrendEnrichError("empty navigation document")
    if _looks_like_challenge(document_text):
        raise TrendChallengeError("login/security checkpoint page — STOP")

    matches: list[tuple[dict, dict]] = []  # (node, parent_lookup)
    pk_by_code: set[str] = set()
    for payload in _bbox_payloads(document_text):
        parent_lookup: dict[int, Any] = {}
        nodes = list(_walk_with_parents(payload))
        for node, parent in nodes:
            parent_lookup[id(node)] = parent
        for node, _parent in nodes:
            if node.get("code") != shortcode:
                continue
            pk = node.get("pk") or node.get("id")
            if pk is None:
                continue
            pk_by_code.add(str(pk))
            matches.append((node, parent_lookup))
        # match-without-pk still counts as "present but unusable"
        for node, _parent in nodes:
            if node.get("code") == shortcode and node.get("pk") is None \
                    and node.get("id") is None:
                pk_by_code.add("<no-pk>")

    if not matches:
        raise TrendEnrichError(
            f"structured preloader present but no post with code {shortcode!r} "
            "(and pk) — fail closed"
        )
    if len(pk_by_code) > 1:
        raise TrendEnrichError(
            f"conflicting structured matches for {shortcode!r}: pks {sorted(pk_by_code)}"
        )

    # Merge across duplicate occurrences: first match wins per field, but a
    # richer later occurrence fills fields the first one lacked.
    merged: dict[str, Any] = {}
    for node, parent_lookup in matches:
        for key, value in _facts_from_node(node, parent_lookup).items():
            merged.setdefault(key, value)
    return merged


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
    facts = extract_post_facts(document_text, shortcode)
    return {"code": shortcode, "pk": facts["source_post_id"]}
