"""Deterministic tests for dm_browser.ThreadsDMPage — the live CDP adapter.

No live Threads: ThreadsDMPage is driven by a stubbed _run() that feeds the
recon-observed DOM JSON shapes back, validating that each DMPage method maps
its page probe to the right verdict and fails closed on unexpected shapes.
"""
import pathlib

import pytest

from threads_operator import dm_browser


def make_page(responses):
    """Build a ThreadsDMPage without opening a browser; _run is stubbed to
    return canned verdicts keyed by a distinctive substring of each JS probe."""
    import asyncio
    page = object.__new__(dm_browser.ThreadsDMPage)
    page.profile_dir = pathlib.Path("/nonexistent")
    page.timeout = 5.0
    page._browser = None
    page._loop = asyncio.new_event_loop()
    page.settle = 0
    calls = []

    def _run(expression):
        calls.append(expression)
        for marker, value in responses:
            if marker in expression:
                return value
        return None

    page._run = _run
    page._calls = calls
    return page


# --------------------------------------------------------------------------- whoami
def test_whoami_returns_username():
    page = make_page([("aria-label", "syaqir")])
    assert page.current_logged_in_username() == "syaqir"


def test_whoami_none_when_absent():
    page = make_page([("aria-label", None)])
    assert page.current_logged_in_username() is None


# --------------------------------------------------------------------------- challenge
def test_challenge_detected():
    page = make_page([("challenge", {"challenge": "captcha"})])
    assert page.detect_challenge() == "captcha"


def test_no_challenge():
    page = make_page([("challenge", {"challenge": None})])
    assert page.detect_challenge() is None


# --------------------------------------------------------------------------- recipients
def test_search_recipients_parses_rows():
    rows = {"rows": [
        {"username": "hanisahnorazman", "display": "HN"},
        {"username": "someone_else", "display": "SE"},
    ]}
    page = make_page([("split('\\n')", rows)])
    result = page.search_recipients("hanisah")
    assert ("hanisahnorazman", "HN") in result
    assert ("someone_else", "SE") in result


def test_search_recipients_empty():
    page = make_page([("split('\\n')", {"rows": []})])
    assert page.search_recipients("nobody") == []


# --------------------------------------------------------------------------- open conversation
def test_open_conversation_exact_match_returns_thread_id():
    page = make_page([
        ("=== target", {"ok": True, "matches": 1}),
        ("location.pathname", {"thread_id": "1290271635938149", "url": "https://www.threads.com/messages/t/1290271635938149"}),
    ])
    tid = page.open_conversation("hanisahnorazman")
    assert tid == "1290271635938149"


def test_open_conversation_ambiguous_returns_none():
    page = make_page([("=== target", {"ok": False, "matches": 2})])
    assert page.open_conversation("hanisahnorazman") is None


def test_open_conversation_not_found_returns_none():
    page = make_page([("=== target", {"ok": False, "matches": 0})])
    assert page.open_conversation("ghost") is None


# --------------------------------------------------------------------------- composer + send
def test_insert_text_ok():
    page = make_page([("insertText", {"ok": True})])
    page.insert_text("hello")  # should not raise


def test_insert_text_no_composer_raises():
    page = make_page([("insertText", {"ok": False, "reason": "no_composer"})])
    with pytest.raises(dm_browser.browser_cdp.BrowserCDPError):
        page.insert_text("hello")


def test_read_composer():
    page = make_page([("contenteditable", {"text": "approved body"})])
    assert page.read_composer() == "approved body"


def test_click_send_no_composer_raises():
    page = make_page([("Enter", {"ok": False, "reason": "no_composer"})])
    with pytest.raises(dm_browser.browser_cdp.BrowserCDPError):
        page.click_send()


# --------------------------------------------------------------------------- confirmation
def test_confirm_outgoing_positive():
    page = make_page([
        ("composerEmpty", {"confirmed": True, "composerEmpty": True}),
        ("location.pathname", {"thread_id": "t123"}),
    ])
    res = page.confirm_latest_outgoing("approved body")
    assert res == {"thread_id": "t123", "evidence": "latest_outgoing_bubble"}


def test_confirm_outgoing_negative_returns_none():
    page = make_page([("composerEmpty", {"confirmed": False, "composerEmpty": False})])
    assert page.confirm_latest_outgoing("approved body") is None


# --------------------------------------------------------------------------- reconcile
def test_reconcile_present_true():
    page = make_page([("present", {"present": True})])
    assert page.recent_outgoing_contains("approved body") is True


def test_reconcile_absent_false():
    page = make_page([("present", {"present": False})])
    assert page.recent_outgoing_contains("approved body") is False
