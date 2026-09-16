"""RED tests: read-only authenticated browser read + end-to-end enrichment flow.

Browser reads must reuse the existing CDP machinery
(browser_cdp.ActivityBrowser); no second browser framework, one clean page
load, private tab closed. Enrichment is fail-closed: no write unless the
structured evidence matches the candidate permalink exactly.
"""

import json
import pathlib

import httpx
import pytest

from threads_operator import trend_browser
from threads_operator.supabase_store import SupabaseStore
from threads_operator import trend_enrich
from threads_operator.trend_enrich import TrendChallengeError, TrendEnrichError

SHORTCODE = "DdGAw6kFfPA"
PERMALINK = f"https://www.threads.com/@syaqir_sharani/post/{SHORTCODE}"
SERVICE_KEY = "service-KEY-not-real"


def _feed_doc(pk: str, code: str = SHORTCODE) -> str:
    post = json.dumps({"pk": pk, "code": code, "text": "kita ni belakang kira"})
    return (
        '{"__bbox":{"status":"success","result":{"data":{"feedData":{"edges":'
        '[{"node":{"text_post_app_thread":' + post + "}}]}}}}}"
    )


class FakeBrowser:
    """Stands in for browser_cdp.ActivityBrowser inside trend_browser."""

    instances: list["FakeBrowser"] = []

    def __init__(self, profile_dir, *, body="", b64=False, status=200, doc_url=None):
        self.profile_dir = pathlib.Path(profile_dir)
        self.body = body
        self.b64 = b64
        self.status = status
        self.doc_url = doc_url or PERMALINK
        self.calls: list[tuple[str, dict]] = []
        self.handlers: list = []
        self.closed = False
        self.session = _FakeSession(self)
        self.session.responses["r1"] = {
            "status": status,
            "url": self.doc_url,
            "mimeType": "text/html",
            "type": "Document",
        }

    async def __aenter__(self):
        FakeBrowser.instances.append(self)
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    @property
    def conn(self):
        owner = self

        class _Conn:
            def add_handler(self, handler):
                owner.handlers.append(handler)

        if not hasattr(self, "_conn"):
            self._conn = _Conn()
        return self._conn

    def navigation_calls(self):
        return [p.get("url") for (m, p) in self.calls if m == "Page.navigate"]


class _FakeSession:
    session_id = "s1"

    def __init__(self, owner):
        self.owner = owner
        self.responses: dict[str, dict] = {}

    async def call(self, method, params=None, timeout=None):
        self.owner.calls.append((method, params or {}))
        if method == "Page.navigate":
            for h in self.owner.handlers:
                h("Page.loadEventFired", {}, "s1")
        return {}

    async def response_body(self, rid):
        return self.owner.body, self.owner.b64


@pytest.fixture(autouse=True)
def _reset_browsers():
    FakeBrowser.instances = []
    yield


def install_fake(monkeypatch, **kwargs):
    monkeypatch.setattr(
        trend_browser, "ActivityBrowser", lambda profile_dir: FakeBrowser(profile_dir, **kwargs)
    )


def read_doc(monkeypatch, **kwargs):
    install_fake(monkeypatch, **kwargs)
    return trend_browser.read_post_document_sync(
        pathlib.Path("/tmp/fake-profile"), PERMALINK, settle_seconds=0.01
    )


# ----------------------------------------------------- browser read behavior


def test_browser_read_navigates_to_permalink_once(monkeypatch):
    doc = read_doc(monkeypatch, body=_feed_doc("1788000000000001"))
    assert len(FakeBrowser.instances) == 1
    brows = FakeBrowser.instances[0]
    assert brows.navigation_calls() == [PERMALINK]
    assert brows.closed is True
    assert "1788000000000001" in doc


def test_browser_read_final_url_home_document_is_authoritative(monkeypatch):
    """Authenticated navigation can end at threads.com root; the captured
    navigation document is what matters."""
    doc = read_doc(monkeypatch, doc_url="https://www.threads.com/", body=_feed_doc("17880909"))
    assert "17880909" in doc


def test_browser_read_base64_body_decoded(monkeypatch):
    import base64 as _b64

    payload = _feed_doc("1788000000000002")
    doc = read_doc(
        monkeypatch, body=_b64.b64encode(payload.encode()).decode(), b64=True
    )
    assert "1788000000000002" in doc


def test_browser_read_challenge_status_raises(monkeypatch):
    with pytest.raises(TrendChallengeError):
        read_doc(monkeypatch, status=302, body="")


def test_browser_read_missing_document_raises(monkeypatch):
    def factory(profile_dir):
        brows = FakeBrowser(profile_dir, body="")
        brows.session.responses = {}
        return brows

    monkeypatch.setattr(trend_browser, "ActivityBrowser", factory)
    with pytest.raises(TrendEnrichError):
        trend_browser.read_post_document_sync(
            pathlib.Path("/tmp/fake-profile"), PERMALINK, settle_seconds=0.01
        )


def test_browser_read_rejects_non_permalink_before_touching_browser(monkeypatch):
    def factory(profile_dir):
        raise AssertionError("browser must not be opened for a non-permalink URL")

    monkeypatch.setattr(trend_browser, "ActivityBrowser", factory)
    with pytest.raises(ValueError):
        trend_browser.read_post_document_sync(
            pathlib.Path("/tmp/fake-profile"),
            "https://evil.example.com/@x/post/ABC",
            settle_seconds=0.01,
        )
    assert FakeBrowser.instances == []


# ----------------------------------------------------- end-to-end enrichment


CANDIDATE = {
    "id": 1,
    "target_account_id": "syaqir",
    "source_permalink": PERMALINK,
    "status": "discovered",
    "source_post_id": None,
}


def make_store(captured, rows_for_get, patch_response=None):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=rows_for_get)
        return httpx.Response(200, json=patch_response or [{**CANDIDATE, "source_post_id": "1788000000000001"}])

    return SupabaseStore(
        "https://example.supabase.co",
        SERVICE_KEY,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        account_key="syaqir",
    )


def stub_reader(monkeypatch, *, doc=None, challenge=False):
    def fake_read(profile_dir, permalink, *, settle_seconds=3.0):
        if challenge:
            raise TrendChallengeError("simulated challenge")
        return doc if doc is not None else _feed_doc("1788000000000001")

    monkeypatch.setattr(trend_enrich, "read_post_document_sync", fake_read)


def test_enrich_writes_source_post_id_on_exact_match(monkeypatch):
    captured: list = []
    store = make_store(captured, [CANDIDATE])
    stub_reader(monkeypatch)

    result = trend_enrich.enrich_candidate(
        store, candidate_id=1, profile_dir=pathlib.Path("/tmp/fake-profile"), settle_seconds=0.01
    )

    assert result["updated"] is True
    assert result["source_post_id"] == "1788000000000001"
    patches = [r for r in captured if r.method == "PATCH"]
    assert len(patches) == 1
    assert json.loads(patches[0].content) == {"source_post_id": "1788000000000001"}
    params = dict(patches[0].url.params)
    assert params["id"] == "eq.1"
    assert params["target_account_id"] == "eq.syaqir"


def test_enrich_no_write_on_shortcode_mismatch(monkeypatch):
    captured: list = []
    store = make_store(captured, [CANDIDATE])
    stub_reader(monkeypatch, doc=_feed_doc("1788999999999999", code="ZZZZZZZZZZZ"))

    with pytest.raises(TrendEnrichError):
        trend_enrich.enrich_candidate(
            store, candidate_id=1, profile_dir=pathlib.Path("/tmp/fake-profile"), settle_seconds=0.01
        )
    assert [r for r in captured if r.method == "PATCH"] == []


def test_enrich_no_write_on_challenge(monkeypatch):
    captured: list = []
    store = make_store(captured, [CANDIDATE])
    stub_reader(monkeypatch, challenge=True)

    with pytest.raises(TrendChallengeError):
        trend_enrich.enrich_candidate(
            store, candidate_id=1, profile_dir=pathlib.Path("/tmp/fake-profile"), settle_seconds=0.01
        )
    assert [r for r in captured if r.method == "PATCH"] == []


def test_enrich_candidate_not_found_is_error_no_write(monkeypatch):
    captured: list = []
    store = make_store(captured, [])
    stub_reader(monkeypatch)

    with pytest.raises(TrendEnrichError):
        trend_enrich.enrich_candidate(
            store, candidate_id=42, profile_dir=pathlib.Path("/tmp/fake-profile"), settle_seconds=0.01
        )
    assert [r for r in captured if r.method == "PATCH"] == []


def test_enrich_already_matching_skips_write(monkeypatch):
    captured: list = []
    row = {**CANDIDATE, "source_post_id": "1788000000000001"}
    store = make_store(captured, [row])
    stub_reader(monkeypatch)

    result = trend_enrich.enrich_candidate(
        store, candidate_id=1, profile_dir=pathlib.Path("/tmp/fake-profile"), settle_seconds=0.01
    )

    assert result["updated"] is False
    assert [r for r in captured if r.method == "PATCH"] == []
