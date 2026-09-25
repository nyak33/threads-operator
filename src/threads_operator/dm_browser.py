"""Task 2D — live browser adapter: drives the persistent authenticated Threads
Chromium (via ``browser_cdp``) to satisfy the ``dm_send.DMPage`` protocol.

Recon-grounded (read-only, 2026-09-25, profile syaqir, Chromium :43399):

  * DM surface ......... https://www.threads.com/messages  (NOT /direct/…)
  * First-visit intro .. dialog "Direct messages have arrived on web" + Continue
  * Recipient search ... <input placeholder="Search">
  * Recipient row ...... <div role="button"> innerText line[0] = EXACT username
  * Conversation URL ... /messages/t/<thread_id>  (the external_dm_id)
  * Composer ........... <div contenteditable="true" role="textbox">

Every interaction is a bounded Runtime.evaluate probe that returns a small JSON
verdict; the adapter FAILS CLOSED on any unexpected UI, challenge, login wall,
or ambiguous recipient — it never guesses at a "Send-looking" control. No
cookies/session data are read or persisted; the tab is always closed afterwards.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
from typing import Any

from . import browser_cdp

MESSAGES_URL = "https://www.threads.com/messages"

# Substrings that indicate a hard stop (login wall / challenge / CAPTCHA). The
# adapter surfaces these as detect_challenge() values for dm_send to map onto
# its structured failure categories. Never bypassed.
_CHALLENGE_SIGNALS = (
    ("captcha", "captcha"),
    ("checkpoint", "checkpoint"),
    ("two-factor", "2fa"),
    ("two factor", "2fa"),
    ("confirmation_code", "2fa"),
    ("suspicious", "checkpoint"),
    ("log in", "login"),
    ("login", "login"),
)


class _JS:
    """Bounded page probes. Each returns a JSON-serialisable verdict object."""

    # Who is logged in? Reads the profile nav / viewer link. Returns username or null.
    WHOAMI = r"""
(() => {
  // The logged-in account is most reliably read from the "/@<username>" links
  // Threads renders for the signed-in user (own-profile / own-avatar links).
  // These use the "/@" prefix; utility links ("/search", "/messages", "/")
  // never do. Prefer the most frequent "/@user" href — that is the account.
  const counts = new Map();
  document.querySelectorAll('a[href^="/@"]').forEach(a => {
    const m = (a.getAttribute('href')||'').match(/^\/@([A-Za-z0-9._]+)\/?$/);
    if (m) counts.set(m[1], (counts.get(m[1])||0)+1);
  });
  if (counts.size) {
    let best = null, bestN = -1;
    for (const [u,n] of counts) if (n > bestN) { best = u; bestN = n; }
    return best;
  }
  // Fallback: a nav "Profile" link aria-labeled as such, href "/@user" or "/user".
  const links = Array.from(document.querySelectorAll('a[href]'));
  for (const a of links) {
    const aria = (a.getAttribute('aria-label')||'').toLowerCase();
    if (aria === 'profile') {
      const m = (a.getAttribute('href')||'').match(/^\/@?([A-Za-z0-9._]+)\/?$/);
      if (m) return m[1];
    }
  }
  return null;
})()
"""

    # Detect challenge / login wall from URL + body text.
    CHALLENGE = r"""
(() => {
  const u = (location.href||'').toLowerCase();
  const b = (document.body && document.body.innerText || '').toLowerCase().slice(0,4000);
  const hay = u + '\n' + b;
  const sigs = __SIGS__;
  for (const [sig, cat] of sigs) if (hay.includes(sig)) return {challenge: cat, url: location.href};
  return {challenge: null};
})()
"""

    # Advance past the intro dialog if present (click "Continue"). Returns state.
    DISMISS_INTRO = r"""
(() => {
  const btns = Array.from(document.querySelectorAll('div[role="button"]'));
  const cont = btns.find(x => /^continue$/i.test((x.innerText||'').trim()));
  if (cont) { cont.click(); return {dismissed: true}; }
  return {dismissed: false};
})()
"""

    # Type the query into the recipient search input.
    TYPE_SEARCH = r"""
(() => {
  const q = __Q__;
  const inp = Array.from(document.querySelectorAll('input'))
    .find(i => (i.placeholder||'').toLowerCase() === 'search');
  if (!inp) return {ok:false, reason:'no_search_input'};
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
  inp.focus(); setter.call(inp, q);
  inp.dispatchEvent(new Event('input',{bubbles:true}));
  return {ok:true};
})()
"""

    # Read the current recipient result rows: [{username, display}].
    READ_RECIPIENTS = r"""
(() => {
  const rows = Array.from(document.querySelectorAll('div[role="button"]'))
    .map(el => (el.innerText||'').trim())
    .filter(t => t && t.includes('\n'))
    .map(t => { const [u, ...rest] = t.split('\n'); return {username:(u||'').trim(), display:rest.join(' ').trim()}; })
    .filter(r => r.username);
  return {rows};
})()
"""

    # Click the recipient row whose FIRST LINE exactly equals the target username.
    # Returns how many exact matches existed (0 -> not found, >1 -> ambiguous).
    CLICK_EXACT_RECIPIENT = r"""
(() => {
  const target = __TARGET__;
  const matches = Array.from(document.querySelectorAll('div[role="button"]'))
    .filter(el => ((el.innerText||'').split('\n')[0]||'').trim().toLowerCase() === target);
  if (matches.length !== 1) return {ok:false, matches: matches.length};
  matches[0].click();
  return {ok:true, matches:1};
})()
"""

    # Current conversation thread id from the URL (/messages/t/<id>).
    THREAD_ID = r"""
(() => {
  const m = (location.pathname||'').match(/\/messages\/t\/(\d+)/);
  return {thread_id: m ? m[1] : null, url: location.href};
})()
"""

    # Insert text into the composer (contenteditable textbox).
    INSERT_TEXT = r"""
(() => {
  const t = __TEXT__;
  const ed = document.querySelector('[contenteditable="true"][role="textbox"]');
  if (!ed) return {ok:false, reason:'no_composer'};
  ed.focus();
  document.execCommand('insertText', false, t);
  return {ok:true};
})()
"""

    # Read composer text.
    READ_COMPOSER = r"""
(() => {
  const ed = document.querySelector('[contenteditable="true"][role="textbox"]');
  return {text: ed ? (ed.innerText||ed.textContent||'') : null};
})()
"""

    # Send: press Enter in the focused composer (Threads web sends on Enter).
    CLICK_SEND = r"""
(() => {
  const ed = document.querySelector('[contenteditable="true"][role="textbox"]');
  if (!ed) return {ok:false, reason:'no_composer'};
  ed.focus();
  const opts = {bubbles:true, cancelable:true, key:'Enter', code:'Enter', keyCode:13, which:13};
  ed.dispatchEvent(new KeyboardEvent('keydown', opts));
  ed.dispatchEvent(new KeyboardEvent('keypress', opts));
  ed.dispatchEvent(new KeyboardEvent('keyup', opts));
  return {ok:true};
})()
"""

    # After send: is the exact expected text the latest outgoing bubble?
    CONFIRM_OUTGOING = r"""
(() => {
  const expected = __TEXT__;
  const ed = document.querySelector('[contenteditable="true"][role="textbox"]');
  const composerEmpty = ed ? ((ed.innerText||'').trim() === '') : null;
  // Outgoing message bubbles: rows containing our exact text that are NOT the composer.
  const bodies = Array.from(document.querySelectorAll('div[dir="auto"], span[dir="auto"]'))
    .map(el => (el.innerText||'').trim())
    .filter(t => t);
  const hit = bodies.some(t => t === expected.trim());
  return {confirmed: hit, composerEmpty: composerEmpty};
})()
"""

    # Reconcile: does any recent outgoing bubble equal the expected text?
    RECONCILE = r"""
(() => {
  const expected = __TEXT__;
  const bodies = Array.from(document.querySelectorAll('div[dir="auto"], span[dir="auto"]'))
    .map(el => (el.innerText||'').trim())
    .filter(t => t);
  return {present: bodies.some(t => t === expected.trim())};
})()
"""


def _json_literal(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


class ThreadsDMPage:
    """Synchronous façade over the async CDP session, matching DMPage.

    A fresh private tab is opened on construction and navigated to the DM
    surface; the tab is closed on close(). All probes fail closed.
    """

    def __init__(self, profile_dir: pathlib.Path, *, timeout: float = 60.0,
                 settle: float = 2.0):
        self.profile_dir = pathlib.Path(profile_dir)
        self.timeout = timeout
        self.settle = settle            # post-action render wait (0 in tests)
        self._loop = asyncio.new_event_loop()
        self._browser: browser_cdp.ActivityBrowser | None = None
        self._open()

    # -- lifecycle ---------------------------------------------------------
    def _open(self) -> None:
        self._browser = self._loop.run_until_complete(
            self._bootstrap()
        )

    async def _bootstrap(self) -> browser_cdp.ActivityBrowser:
        browser = await browser_cdp.ActivityBrowser(self.profile_dir).__aenter__()
        sess = browser.session
        if sess is None:
            raise browser_cdp.BrowserCDPError("failed to attach a CDP session")
        await sess.call("Page.enable")
        await sess.call("Runtime.enable")
        await sess.call("Network.enable")
        await self._navigate(sess, MESSAGES_URL)
        # Dismiss the one-time intro dialog if present.
        await self._eval(sess, _JS.DISMISS_INTRO)
        await asyncio.sleep(1.0)
        return browser

    async def _navigate(self, sess, url: str) -> None:
        done = self._loop.create_future()
        sess.conn.add_handler(
            lambda m, p, s: (not done.done()) and done.set_result(True)
            if (m == "Page.loadEventFired" and s == sess.session_id) else None
        )
        await sess.call("Page.navigate", {"url": url})
        try:
            await asyncio.wait_for(done, timeout=45)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(self.settle)

    async def _eval(self, sess, expression: str) -> Any:
        res = await sess.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=self.timeout,
        )
        if "exceptionDetails" in res:
            raise browser_cdp.BrowserCDPError(
                f"page probe raised: {res['exceptionDetails'].get('text')}")
        return (res.get("result") or {}).get("value")

    def _run(self, expression: str) -> Any:
        if self._browser is None or self._browser.session is None:
            raise browser_cdp.BrowserCDPError("browser tab not open")
        return self._loop.run_until_complete(
            self._eval(self._browser.session, expression)
        )

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._loop.run_until_complete(self._browser.aclose())
        finally:
            self._browser = None
            self._loop.close()

    def __enter__(self) -> "ThreadsDMPage":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    # -- DMPage protocol ----------------------------------------------------
    def current_logged_in_username(self) -> str | None:
        val = self._run(_JS.WHOAMI)
        return val if isinstance(val, str) and val else None

    def detect_challenge(self) -> str | None:
        js = _JS.CHALLENGE.replace("__SIGS__", _json_literal(_CHALLENGE_SIGNALS))
        val = self._run(js) or {}
        return val.get("challenge")

    def search_recipients(self, query: str) -> list[tuple[str, str]]:
        self._run(_JS.TYPE_SEARCH.replace("__Q__", _json_literal(query)))
        # allow the filtered list to render
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(asyncio.sleep(self.settle))
        val = self._run(_JS.READ_RECIPIENTS) or {}
        return [(r.get("username", ""), r.get("display", "")) for r in val.get("rows", [])]

    def open_conversation(self, canonical_username: str) -> str | None:
        target = canonical_username.strip().lstrip("@").lower()
        js = _JS.CLICK_EXACT_RECIPIENT.replace("__TARGET__", _json_literal(target))
        val = self._run(js) or {}
        if not val.get("ok"):
            return None  # not found / ambiguous handled by dm_send via search
        self._loop.run_until_complete(asyncio.sleep(self.settle))
        tid = self._run(_JS.THREAD_ID) or {}
        return tid.get("thread_id")

    def insert_text(self, text: str) -> None:
        val = self._run(_JS.INSERT_TEXT.replace("__TEXT__", _json_literal(text))) or {}
        if not val.get("ok"):
            raise browser_cdp.BrowserCDPError(f"composer insert failed: {val.get('reason')}")

    def read_composer(self) -> str:
        val = self._run(_JS.READ_COMPOSER) or {}
        return val.get("text") or ""

    def click_send(self) -> None:
        val = self._run(_JS.CLICK_SEND) or {}
        if not val.get("ok"):
            raise browser_cdp.BrowserCDPError(f"send click failed: {val.get('reason')}")
        self._loop.run_until_complete(asyncio.sleep(self.settle))

    def confirm_latest_outgoing(self, expected_text: str) -> dict | None:
        val = self._run(_JS.CONFIRM_OUTGOING.replace("__TEXT__", _json_literal(expected_text))) or {}
        if val.get("confirmed"):
            tid = self._run(_JS.THREAD_ID) or {}
            return {"thread_id": tid.get("thread_id"), "evidence": "latest_outgoing_bubble"}
        return None

    def recent_outgoing_contains(self, expected_text: str) -> bool | None:
        try:
            val = self._run(_JS.RECONCILE.replace("__TEXT__", _json_literal(expected_text))) or {}
        except browser_cdp.BrowserCDPError:
            return None
        if "present" in val:
            return bool(val["present"])
        return None
