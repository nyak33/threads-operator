"""Deterministic text normalization and fingerprinting for Activity attribution."""
from __future__ import annotations

import hashlib
import re
import unicodedata

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"))
_TRAILING_ELLIPSIS = re.compile(r"(?:\s*(?:\.\.\.|…+))\s*$")
_WHITESPACE = re.compile(r"\s+")
_ARABIC_MARKS = re.compile(r"[\u064B-\u0652\u0670\u0640]")


def normalize_snippet(text) -> str:
    """Normalize a raw snippet or post text for comparison."""
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", text)
    out = out.translate(_ZERO_WIDTH)
    out = _ARABIC_MARKS.sub("", out)
    out = _WHITESPACE.sub(" ", out)
    out = out.casefold().strip()
    while True:
        trimmed = _TRAILING_ELLIPSIS.sub("", out).strip()
        if trimmed == out:
            break
        out = trimmed
    out = out.rstrip(" \t.!?,:;—–-»\"'）)】」》")
    return out.strip()


def snippet_hash(text: str) -> str:
    """SHA-256 digest of normalized text."""
    return hashlib.sha256(normalize_snippet(text).encode("utf-8")).hexdigest()


def fallback_fingerprint(notification_datetime: str, source_snippet_raw: str,
                         visible_username: str,
                         grouped_others_count: int) -> str:
    """Deterministic fallback identity for deduplication."""
    payload = "|".join([
        (notification_datetime or "")[:19],
        normalize_snippet(source_snippet_raw or ""),
        (visible_username or "").casefold(),
        str(int(grouped_others_count or 0)),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
