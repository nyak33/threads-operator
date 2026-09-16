"""Deterministic normalization/validation of Threads post permalinks.

Pure string logic only: no browser, no Threads API, no LLM. Used by trend
candidate ingress so equivalent URLs collapse to one canonical permalink and
non-Threads or spoofed hosts are rejected before any Supabase write.

Two identity forms are supported (live-proven, see test docstrings):
A. shortcode permalinks:      /@user/post/DdGAw6kFfPA   (base64-ish code)
B. numeric (fbid) permalinks: /@user/post/17909304312530892
   https://www.threads.net/@user/post/<numeric> 301-redirects host-only to
   https://www.threads.com/@user/post/<numeric> — the slug is intact, so
   both spellings denote the same post and canonicalize to one URL.

The two forms are DIFFERENT identity spaces: no local numeric->shortcode
conversion exists or is attempted. Bridging (Activity numeric ids to the
structured web shortcode) is done exclusively via the official Graph API
with exact id-echo validation, in trend_activity.py.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_ALLOWED_HOSTS = {
    "threads.com", "www.threads.com", "m.threads.com",
    "threads.net", "www.threads.net",
}
_CANONICAL_HOST = "www.threads.com"
_NUMERIC_RE = re.compile(r"^[0-9]{8,25}$")
# Any code containing letters keeps the historical permissive shape; short-
# form validation would be a behavior change outside this goal's scope.
_SHORTCODE_RE = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9_-]+$")
_POST_PATH_RE = re.compile(
    r"^/@(?P<username>[A-Za-z0-9_][A-Za-z0-9._]*)/post/(?P<code>[A-Za-z0-9_-]+)/?$"
)


def _validate_identifier(code: str, host: str) -> str:
    """Accept only well-formed post identifiers; reject near-miss junk.

    On the threads.net family only pure-numeric Activity ids are legitimate.
    On threads.com, pure-numeric ids are accepted (301-target equivalence)
    and shortcode-shaped codes are accepted; anything else (too short, too
    long, zero-width junk) is rejected — the old regex accepted garbage.
    """
    if _NUMERIC_RE.match(code):
        return code
    if host.endswith("threads.net"):
        raise ValueError(
            "threads.net permalinks must carry a pure numeric post id")
    if _SHORTCODE_RE.match(code):
        return code
    raise ValueError(
        "post identifier is neither a numeric Activity id nor a "
        "shortcode-shaped code")


def normalize_threads_post_url(url: str) -> tuple[str, str | None]:
    """Return (canonical_permalink, source_username_or_None).

    Canonical form: https://www.threads.com/@<username>/post/<identifier>
    - requires https and an exact threads.com/threads.net host (no suffix
      tricks; both families 301 to the same content per live probe)
    - strips query parameters (tracking) and fragments
    - strips a trailing slash
    - lowercases the @username segment (Threads handles are
      case-insensitive) while preserving the post identifier verbatim
      (shortcodes ARE case-sensitive; numerics are digits by definition)
    Raises ValueError on anything that is not a Threads post permalink.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL must be a non-empty string")
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise ValueError("Only https:// Threads post URLs are accepted")
    host = (parts.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise ValueError(f"Host {host!r} is not a Threads host")
    if parts.username or parts.password or parts.port:
        raise ValueError("URL userinfo/port are not allowed")
    match = _POST_PATH_RE.match(parts.path)
    if not match:
        raise ValueError(
            "URL is not a Threads post permalink "
            "(@username/post/<code> path required)"
        )
    username = match.group("username").lower()
    code = _validate_identifier(match.group("code"), host)
    canonical = f"https://{_CANONICAL_HOST}/@{username}/post/{code}"
    return canonical, username


def identity_forms(permalink: str) -> dict[str, str]:
    """Classify a (possibly raw) permalink's post identifier.

    Returns {"kind": "numeric"|"shortcode", "username": <lowercased>, ...}
    plus "numeric_id" or "shortcode" carrying the identifier VERBATIM.
    Never fabricates the other form. Raises ValueError on invalid URLs.
    """
    canonical, username = normalize_threads_post_url(permalink)
    code = canonical.rsplit("/post/", 1)[1]
    forms: dict[str, str] = {"kind": "numeric" if code.isdigit() else "shortcode",
                             "username": username or ""}
    if forms["kind"] == "numeric":
        forms["numeric_id"] = code
    else:
        forms["shortcode"] = code
    return forms


def permalinks_reference_same_post(a: str, b: str) -> bool:
    """Deterministic same-post test WITHOUT any cross-form conversion.

    True iff both canonicalize (host unified, username lowercased) to the
    exact same identifier. A numeric URL and a shortcode URL are NEVER
    claimed equal here — that equivalence must be proven by the official
    Graph API bridge, not by string inspection.
    """
    ca, _ = normalize_threads_post_url(a)
    cb, _ = normalize_threads_post_url(b)
    return ca == cb
