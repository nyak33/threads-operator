"""Read-only browser reader for Threads Activity → Follows.

Uses the ALREADY-RUNNING persistent authenticated Chromium profile via CDP.
One clean page load per run. Extracts the server-prefetched
``BarcelonaActivityFeedStoryListContainerQuery`` JSON directly from the
Document response (verified: the Activity feed is hydrated entirely from the
document; no XHR/GraphQL replay needed).

Hard safety rules:
- navigates only https://www.threads.com/activity/follows
- never clicks, types, scrolls, opens follower profiles, or replays requests
- never reads cookies, localStorage, or credentials
- closes its private tab on exit
- raises ActivityChallengeError on login/CAPTCHA/2FA/security walls
"""
from __future__ import annotations

import asyncio
import base64
import json
import pathlib
from typing import Any

from .browser_cdp import ActivityBrowser, BrowserCDPError

ACTIVITY_URL = "https://www.threads.com/activity/follows"
_PRELOADER_KEY = ("BarcelonaActivityFeedStoryListContainerQuery"
                  "RelayPreloader")
_CHALLENGE_MARKERS = (
    "checkpoint", "login?next", "/login/", "suspicious",
    "two-factor", "confirmation_code", "captcha", "unsupported_browser",
)


class ActivityChallengeError(RuntimeError):
    """Authentication/security challenge appeared. STOP; do not bypass."""


class ActivityReadError(RuntimeError):
    """Reader failed to obtain the Activity payload (no challenge seen)."""


def _extract_preloader_payload(document_text: str) -> Any:
    """Pull the Relay preloader JSON out of the document body.

    Brace-matching walk over raw text avoids large JSON dependencies here.
    """
    idx = document_text.find(_PRELOADER_KEY)
    if idx == -1:
        return None
    res_idx = document_text.find('"result"', idx)
    if res_idx == -1:
        return None
    colon = document_text.find(":", res_idx)
    start = document_text.find("{", colon)
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for pos in range(start, len(document_text)):
        ch = document_text[pos]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = document_text[start:pos + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError as exc:
                    raise ActivityReadError(
                        f"preloader JSON unparsable: {exc}") from exc
    return None


async def read_activity_follows(profile_dir: pathlib.Path,
                                settle_seconds: float = 3.0) -> dict:
    """One clean read of /activity/follows. Returns {payload, http_status}.

    Raises ActivityChallengeError on auth walls, ActivityReadError otherwise.
    Caller must treat any exception as non-fatal to the rest of the system.
    """
    async with ActivityBrowser(pathlib.Path(profile_dir)) as brows:
        conn = brows.conn  # pyright: ignore[reportOptionalMemberAccess]
        sess = brows.session  # pyright: ignore[reportOptionalMemberAccess]
        sid = sess.session_id
        doc_id: str | None = None
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
        await sess.call("Page.navigate", {"url": ACTIVITY_URL})
        try:
            await asyncio.wait_for(loaded, timeout=60)
        except asyncio.TimeoutError as exc:
            raise ActivityReadError("page load timed out") from exc

        await asyncio.sleep(settle_seconds)

        for rid, info in sess.responses.items():
            url_clean = (info.get("url") or "").split("?")[0].rstrip("/")
            if url_clean == ACTIVITY_URL.rstrip("/") \
                    and info.get("type") == "Document":
                doc_id = rid
                break

        if doc_id is None:
            raise ActivityReadError("Activity document response not seen")

        status = sess.responses[doc_id].get("status", 0)
        if status in (302, 401, 403):
            raise ActivityChallengeError(
                f"Activity returned HTTP {status} — auth challenge likely")
        if status != 200:
            raise ActivityReadError(f"Activity document HTTP {status}")

        body, was_b64 = await sess.response_body(doc_id)
        text = (base64.b64decode(body).decode("utf-8", "replace")
                if was_b64 else body)

        low = text[:60000].lower()
        url_lower = (sess.responses[doc_id].get("url") or "").lower()
        if any(marker in url_lower for marker in _CHALLENGE_MARKERS):
            raise ActivityChallengeError(
                "redirected to a login/security checkpoint — STOP")
        if "login" in low and "notifications" not in low \
                and _PRELOADER_KEY not in text:
            raise ActivityChallengeError(
                "login wall detected — STOP, do not bypass")

        payload = _extract_preloader_payload(text)
        if payload is None:
            if _PRELOADER_KEY not in text:
                raise ActivityReadError(
                    "Activity preloader absent — schema drift or session expired")
            raise ActivityReadError("preloader present but result unparsable")

        return {"payload": payload, "http_status": status,
                "document_bytes": len(text)}


def read_activity_follows_sync(profile_dir: pathlib.Path,
                               settle_seconds: float = 3.0) -> dict:
    """Sync wrapper (called by CLI scripts)."""
    return asyncio.run(read_activity_follows(profile_dir, settle_seconds))
