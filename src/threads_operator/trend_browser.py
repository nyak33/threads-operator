"""READ-ONLY authenticated browser read of one Threads post permalink.

Mirrors the proven safety rules of activity_browser.py:
- uses the ALREADY-RUNNING persistent Chromium profile via CDP
  (browser_cdp.ActivityBrowser); never launches or kills browsers,
- ONE clean page load per call, private tab closed on exit,
- never clicks, types, scrolls, reads cookies/localStorage/credentials,
- raises TrendChallengeError on login/CAPTCHA/2FA/security walls,
- TrendEnrichError on any other read failure.

The final URL is NOT trusted: authenticated navigation may land on
https://www.threads.com/ while the navigation document (the URL we asked
for) is authoritative.
"""
from __future__ import annotations

import asyncio
import base64
import pathlib
from urllib.parse import urlsplit

from .browser_cdp import ActivityBrowser, BrowserCDPError
from .trend_enrich_extract import (
    TrendChallengeError,
    TrendEnrichError,
    expected_shortcode,
)

_TRUSTED_HOSTS = {"threads.com", "www.threads.com", "m.threads.com"}

__all__ = ["read_post_document_sync", "TrendChallengeError", "TrendEnrichError"]


async def read_post_document(profile_dir: pathlib.Path,
                             permalink: str,
                             settle_seconds: float = 3.0) -> str:
    """Navigate once to `permalink`, return the navigation-document text.

    Fails closed via TrendEnrichError / TrendChallengeError.
    """
    # Validate BEFORE touching the browser: never navigate anywhere else.
    expected_shortcode(permalink)

    async with ActivityBrowser(pathlib.Path(profile_dir)) as brows:
        conn = brows.conn  # pyright: ignore[reportOptionalMemberAccess]
        sess = brows.session  # pyright: ignore[reportOptionalMemberAccess]
        sid = sess.session_id
        loaded = asyncio.get_running_loop().create_future()

        def on_load(params: dict) -> None:
            if not loaded.done():
                loaded.set_result(True)

        conn.add_handler(
            lambda m, p, s: on_load(p) if (
                m == "Page.loadEventFired" and s == sid) else None
        )

        await sess.call("Page.enable")
        await sess.call("Network.enable")
        await sess.call("Page.navigate", {"url": permalink})
        try:
            await asyncio.wait_for(loaded, timeout=60)
        except asyncio.TimeoutError as exc:
            raise TrendEnrichError("page load timed out") from exc

        await asyncio.sleep(settle_seconds)

        canonical = permalink.rstrip("/")
        doc_id: str | None = None
        first_doc_id: str | None = None
        for rid, info in sess.responses.items():
            if info.get("type") != "Document":
                continue
            if first_doc_id is None:
                first_doc_id = rid
            url_clean = (info.get("url") or "").split("?")[0].rstrip("/")
            if url_clean == canonical:
                doc_id = rid
                break
        if doc_id is None:
            # Authenticated navigation can server-redirect to the threads.com
            # root while the captured navigation document is still the
            # authoritative page. This tab performed exactly ONE navigation,
            # so any single Document response is that navigation's document —
            # but only accept it when it is still on a threads.com host.
            if first_doc_id is not None:
                seen_url = (sess.responses[first_doc_id].get("url") or "")
                host = (urlsplit(seen_url).hostname or "").lower()
                if host in _TRUSTED_HOSTS:
                    doc_id = first_doc_id

        if doc_id is None:
            raise TrendEnrichError("post document response not seen")

        status = sess.responses[doc_id].get("status", 0)
        if status in (301, 302, 307, 401, 403):
            raise TrendChallengeError(
                f"post document returned HTTP {status} — auth challenge likely")
        if status != 200:
            raise TrendEnrichError(f"post document HTTP {status}")

        body, was_b64 = await sess.response_body(doc_id)
        text = (base64.b64decode(body).decode("utf-8", "replace")
                if was_b64 else body)
        if not text:
            raise TrendEnrichError("post document body empty")
        return text


def read_post_document_sync(profile_dir: pathlib.Path,
                            permalink: str,
                            settle_seconds: float = 3.0) -> str:
    """Sync wrapper (same shape as read_activity_follows_sync)."""
    return asyncio.run(read_post_document(profile_dir, permalink, settle_seconds))
