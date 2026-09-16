"""Deterministic normalization/validation of public Threads post permalinks.

Pure string logic only: no browser, no Threads API, no LLM. Used by trend
candidate ingress so equivalent URLs collapse to one canonical permalink and
non-Threads or spoofed hosts are rejected before any Supabase write.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_ALLOWED_HOSTS = {"threads.com", "www.threads.com", "m.threads.com"}
_POST_PATH_RE = re.compile(
    r"^/@(?P<username>[A-Za-z0-9_][A-Za-z0-9._]*)/post/(?P<code>[A-Za-z0-9_-]+)/?$"
)


def normalize_threads_post_url(url: str) -> tuple[str, str | None]:
    """Return (canonical_permalink, source_username_or_None).

    Canonical form: https://www.threads.com/@<username>/post/<code>
    - requires https and an exact threads.com host (no suffix tricks)
    - strips query parameters (tracking) and fragments
    - strips a trailing slash
    - lowercases the @username segment (Threads handles are
      case-insensitive) while preserving the post code verbatim; if the
      handle case ever matters, lowercase username is stored separately
    Raises ValueError on anything that is not a canonical Threads post URL.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL must be a non-empty string")
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise ValueError("Only https:// Threads post URLs are accepted")
    host = (parts.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise ValueError(f"Host {host!r} is not threads.com")
    if parts.username or parts.password or parts.port:
        raise ValueError("URL userinfo/port are not allowed")
    match = _POST_PATH_RE.match(parts.path)
    if not match:
        raise ValueError(
            "URL is not a Threads post permalink "
            "(@username/post/<code> path required)"
        )
    username = match.group("username").lower()
    code = match.group("code")
    canonical = f"https://www.threads.com/@{username}/post/{code}"
    return canonical, username
