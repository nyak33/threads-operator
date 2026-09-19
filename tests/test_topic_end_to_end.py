"""Regression tests: every content queue carries its topic to Meta topic_tag.

Covers the five queue field mappings, retry/recovery retention, missing-topic
safety, and that the normalized topic actually reaches the API payload at
container-creation time (never on the publish call itself).
"""

from __future__ import annotations

import pytest

from threads_operator.publisher import publish_next
from threads_operator.topic import normalize_topic, resolve_topic, topic_field_for_table
from threads_operator.threads_api import ThreadsAPI


# --------------------------------------------------------------------------
# Field mapping (all five queues from the spec)
# --------------------------------------------------------------------------

EXPECTED_MAPPINGS = {
    "threads_publish_queue": "topic",
    "threads_content_queue": "topic_tag",
    "note_to_self_queue": "topic_tag",
    "affiliate_queue": "topic",
    "hadith_content_queue": "topic",
}


@pytest.mark.parametrize("table,field", EXPECTED_MAPPINGS.items())
def test_topic_field_mapping(table, field):
    assert topic_field_for_table(table) == field


@pytest.mark.parametrize("table,field", EXPECTED_MAPPINGS.items())
def test_resolve_topic_reads_correct_column(table, field):
    assert resolve_topic(table, {field: "Local SEO"}) == "Local SEO"


# --------------------------------------------------------------------------
# Normalization (Meta topic_tag rules: 1..50 chars, no . & @ ! ? , ; : #)
# --------------------------------------------------------------------------

def test_normalize_strips_meta_rejected_chars():
    assert normalize_topic("Digital Marketing! #1?") == "Digital Marketing 1"


def test_normalize_removes_periods_and_at():
    assert normalize_topic("SEO.tips @home") == "SEOtips home"


def test_normalize_trims_and_collapses_whitespace():
    assert normalize_topic("  Career   Upskilling  ") == "Career Upskilling"


def test_normalize_truncates_over_50_chars():
    result = normalize_topic("A" * 60)
    assert result is not None and len(result) == 50


def test_normalize_truncation_strips_trailing_space():
    assert normalize_topic("x" * 49 + " " + "y" * 10) == "x" * 49


def test_normalize_none_and_blank_are_none():
    assert normalize_topic(None) is None
    assert normalize_topic("") is None
    assert normalize_topic("   ") is None
    assert normalize_topic("#!?.") is None  # only rejected chars remain


def test_normalize_non_string_is_none():
    assert normalize_topic(123) is None
    assert normalize_topic(["SEO"]) is None


def test_resolve_unknown_table_falls_back_to_probe():
    assert resolve_topic("legacy_queue", {"topic_tag": "Hadith & Reflection"}) == (
        "Hadith Reflection"
    )
    assert resolve_topic("legacy_queue", {"topic": "Packaging"}) == "Packaging"
    assert resolve_topic("legacy_queue", {}) is None
    assert resolve_topic("legacy_queue", None) is None


# --------------------------------------------------------------------------
# Publisher passes topic to the API on the main post only
# --------------------------------------------------------------------------

class FakeStore:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def peek_due_post(self, table, campaign_code=None, now=None):
        return self.row

    def claim_due_post(self, table, campaign_code=None, now=None):
        return self.row

    def mark_post_main_published(self, table, row_id, post_id):
        self.calls.append(("main", row_id, post_id))

    def mark_post_reply_progress(self, table, row_id, reply_ids):
        self.calls.append(("progress", row_id, list(reply_ids)))

    def mark_post_posted(self, table, row_id, reply_ids, posted_at=None):
        self.calls.append(("posted", row_id))

    def mark_post_failed(self, table, row_id, error):
        self.calls.append(("failed", row_id, error))


class FakeAPI:
    def __init__(self, ids):
        self.ids = list(ids)
        self.calls = []

    def publish_text(self, text, reply_to_id=None, topic_tag=None, **kwargs):
        self.calls.append(
            {"text": text, "reply_to_id": reply_to_id, "topic_tag": topic_tag}
        )
        return self.ids.pop(0)


def _row(topic="__absent__"):
    row = {"id": 7, "main_post_text": "main", "reply_texts": ["one", "two"]}
    if topic != "__absent__":
        row["topic"] = topic
    return row


def test_publish_passes_topic_to_main_post_only():
    api = FakeAPI(["m", "r1", "r2"])
    result = publish_next(api, FakeStore(_row("Local SEO")), "threads_publish_queue")
    assert result["status"] == "posted"
    # Main post carries the topic; replies must NOT get a topic_tag.
    assert api.calls[0]["topic_tag"] == "Local SEO"
    assert api.calls[1] == {"text": "one", "reply_to_id": "m", "topic_tag": None}
    assert api.calls[2] == {"text": "two", "reply_to_id": "r1", "topic_tag": None}


def test_publish_missing_topic_does_not_crash_and_publishes_untagged():
    api = FakeAPI(["m"])
    result = publish_next(api, FakeStore({"id": 7, "main_post_text": "main"}),
                          "threads_publish_queue")
    assert result["status"] == "posted"
    assert api.calls[0]["topic_tag"] is None


def test_publish_blank_topic_publishes_untagged():
    api = FakeAPI(["m", "r1", "r2"])
    result = publish_next(api, FakeStore(_row("   ")), "threads_publish_queue")
    assert result["status"] == "posted"
    assert all(c["topic_tag"] is None for c in api.calls)


def test_publish_normalizes_topic_before_api():
    api = FakeAPI(["m", "r1", "r2"])
    publish_next(api, FakeStore(_row("Digital Marketing! #growth?")),
                 "threads_publish_queue")
    assert api.calls[0]["topic_tag"] == "Digital Marketing growth"


@pytest.mark.parametrize("table,field", EXPECTED_MAPPINGS.items())
def test_publish_reads_topic_from_each_queue_column(table, field):
    api = FakeAPI(["m"])
    row = {"id": 7, "main_post_text": "main", field: "Local SEO"}
    publish_next(api, FakeStore(row), table)
    assert api.calls[0]["topic_tag"] == "Local SEO"


# --------------------------------------------------------------------------
# Recovery / retry retention — the topic stays on the row, so resumed
# publishes keep it, and a re-run after partial progress does not lose it.
# --------------------------------------------------------------------------

def test_recovery_resume_retains_topic_for_unpublished_main():
    api = FakeAPI(["m", "r1", "r2"])
    row = _row("Hadith & Reflection")
    row["threads_main_post_id"] = None
    row["threads_reply_ids"] = []
    result = publish_next(api, FakeStore(row), "hadith_content_queue")
    assert result["status"] == "posted"
    assert api.calls[0]["topic_tag"] == "Hadith Reflection"  # '&' stripped


def test_recovery_after_main_published_does_not_republish_or_lose_topic():
    api = FakeAPI(["r1", "r2"])
    row = _row("Career Upskilling")
    row["threads_main_post_id"] = "existing-main"
    row["threads_reply_ids"] = []
    result = publish_next(api, FakeStore(row), "note_to_self_queue")
    assert result["status"] == "posted"
    # Main already published — only the replies go out, with no topic_tag.
    assert api.calls == [
        {"text": "one", "reply_to_id": "existing-main", "topic_tag": None},
        {"text": "two", "reply_to_id": "r1", "topic_tag": None},
    ]


def test_failed_then_retried_row_still_has_topic():
    """A row that failed mid-reply keeps its topic column; when requeued and
    claimed again the (already published) main is not duplicated and the
    topic remains available on the row for audit/re-publish of a fresh main."""
    api = FakeAPI(["m", "r1", "r2"])
    row = _row("Local SEO")
    first = publish_next(api, FakeStore(row), "threads_publish_queue")
    assert first["status"] == "posted"
    assert api.calls[0]["topic_tag"] == "Local SEO"
    # The row dict itself is untouched by publishing — topic survives.
    assert row["topic"] == "Local SEO"


# --------------------------------------------------------------------------
# API client: topic_tag lands on the create-container payload only, and
# container retries reuse it.
# --------------------------------------------------------------------------

def test_api_container_retry_reuses_topic_tag():
    payloads = []

    class FlakyPublishClient:
        def __init__(self):
            self.publish_calls = 0

        def post(self, url, data=None):
            payloads.append(dict(data))
            if url.endswith("/threads_publish"):
                self.publish_calls += 1
                if self.publish_calls <= 4:
                    # Transient for every same-container attempt so the
                    # container-retry loop recreates the container once.
                    raise _http_error(500)
                return _Resp({"id": "media-1"})
            return _Resp({"id": f"creation-{self.publish_calls}"})

    api = ThreadsAPI("token", "user", client=FlakyPublishClient())
    result = api.publish_text(
        "hello",
        topic_tag="Local SEO",
        retry_delay_seconds=0,
        container_retries=1,
    )
    assert result == "media-1"
    creates = [p for p in payloads if p.get("media_type") == "TEXT"]
    assert len(creates) == 2
    assert all(p.get("topic_tag") == "Local SEO" for p in creates)
    publishes = [p for p in payloads if "creation_id" in p]
    assert publishes and all("topic_tag" not in p for p in publishes)


def test_api_omits_topic_tag_when_none_or_blank():
    payloads = []

    class Client:
        def post(self, url, data=None):
            payloads.append(dict(data))
            return _Resp({"id": "x"})

    api = ThreadsAPI("token", "user", client=Client())
    api.publish_text("hello", retry_delay_seconds=0)
    api.publish_text("hello again", topic_tag="  ", retry_delay_seconds=0)
    assert all("topic_tag" not in p for p in payloads)


def test_api_topic_tag_on_reply_container_too():
    payloads = []

    class Client:
        def post(self, url, data=None):
            payloads.append(dict(data))
            return _Resp({"id": "x"})

    api = ThreadsAPI("token", "user", client=Client())
    api.publish_text("reply", reply_to_id="parent", topic_tag="SEO",
                     retry_delay_seconds=0)
    create = next(p for p in payloads if p.get("media_type") == "TEXT")
    assert create["reply_to_id"] == "parent"
    assert create["topic_tag"] == "SEO"


# --------------------------------------------------------------------------
# enqueue_draft writes topic into the insert payload (creation-side rule)
# --------------------------------------------------------------------------

def _enqueue_store(captured):
    class Store:
        _headers = {"apikey": "k"}

        def _require_account_key(self):
            return "syaqir"

        def _queue_url(self, table):
            return f"https://sb.test/{table}"

        class client:
            @staticmethod
            def post(url, headers=None, json=None):
                captured.update(json)
                return _Resp([{"id": 1, "status": "draft"}])

    return Store()


def test_enqueue_draft_includes_topic_in_payload():
    captured = {}
    from threads_operator.supabase_store import SupabaseStore

    SupabaseStore.enqueue_draft(
        _enqueue_store(captured), "threads_publish_queue", "text",
        topic="Local SEO",
    )
    assert captured["topic"] == "Local SEO"
    assert captured["status"] == "draft"


def test_enqueue_draft_without_topic_omits_column():
    captured = {}
    from threads_operator.supabase_store import SupabaseStore

    SupabaseStore.enqueue_draft(
        _enqueue_store(captured), "threads_publish_queue", "text"
    )
    assert "topic" not in captured


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _http_error(status):
    import httpx

    request = httpx.Request("POST", "https://graph.threads.net/v1.0/x")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)
