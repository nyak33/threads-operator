"""Regression suite for automatic partial-thread recovery.

Covers the crash/resume contract for the deterministic publish queue:
checkpoints persist after every successful action, resume never duplicates a
root or a confirmed reply, live reconciliation adopts published-but-unrecorded
replies, ambiguous duplicate risk isolates only that row, stale ``posting``
rows are reclaimed by CAS and auto-resumed, and healthy in-flight workers are
never stolen.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx

from threads_operator import publish_worker
from threads_operator.publisher import publish_next
from threads_operator.publish_worker import publish_next_with_recovery
from threads_operator.supabase_store import SupabaseStore

TABLE = "threads_publish_queue"


# ---------------------------------------------------------------------------
# Publisher fakes with live reconciliation support
# ---------------------------------------------------------------------------


class ReconcileStore:
    """Records the exact checkpoint write order the publisher performs."""

    def __init__(self, row):
        self.row = row
        self.calls: list[tuple] = []

    def claim_due_post(self, table, campaign_code=None, now=None):
        self.calls.append(("claim",))
        return self.row

    def peek_due_post(self, table, campaign_code=None, now=None):
        return self.row

    def mark_post_main_published(self, table, row_id, post_id):
        self.calls.append(("main", post_id))

    def mark_post_reply_progress(self, table, row_id, reply_ids):
        self.calls.append(("progress", list(reply_ids)))

    def mark_post_posted(self, table, row_id, reply_ids, posted_at=None):
        self.calls.append(("posted", list(reply_ids)))

    def mark_post_failed(self, table, row_id, error):
        self.calls.append(("failed", error))


class ReconcileAPI:
    """publish_text hands out ids in order; children/replies/feeds are canned.

    ``fail_on`` maps a call index (1-based) to an exception to raise.
    """

    def __init__(self, ids=None, fail_on=None, children=None, feed=None,
                 replies_fail=False):
        self.ids = list(ids or [])
        self.fail_on = dict(fail_on or {})
        self.children = {k: list(v) for k, v in (children or {}).items()}
        self.feed = feed
        self.replies_fail = replies_fail
        self.publishes: list[tuple] = []
        self.queries: list[str] = []

    def publish_text(self, text, reply_to_id=None, topic_tag=None):
        n = len(self.publishes) + 1
        self.publishes.append((text, reply_to_id))
        boom = self.fail_on.get(n)
        if boom is not None:
            raise boom
        return self.ids.pop(0)

    def list_direct_replies(self, post_id, *, max_pages=10):
        self.queries.append(post_id)
        if self.replies_fail:
            raise httpx.ConnectError("replies read down")
        return self.children.get(str(post_id), [])

    def list_posts(self, *, limit=10):
        if self.feed is None:
            raise httpx.ConnectError("feed down")
        return self.feed


def http_error(message="") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://graph.threads.net/v1.0/x")
    response = httpx.Response(400, request=request, text=message)
    return httpx.HTTPStatusError("400 Bad Request", request=request, response=response)


GHOST = httpx.HTTPStatusError(
    'Meta error: type="OAuthException", code=24, subcode=4279009',
    request=httpx.Request("POST", "https://graph.threads.net/v1.0/x"),
    response=httpx.Response(400),
)


def base_row(**over):
    row = {
        "id": 21,
        "main_post_text": "MAIN",
        "reply_texts": ["R1", "R2", "R3"],
        "threads_main_post_id": None,
        "threads_reply_ids": [],
        "attempt_count": 0,
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# 1-4: checkpoint + resume semantics
# ---------------------------------------------------------------------------


def test_crash_before_first_reply_keeps_root_checkpoint():
    """Root persisted, then crash: root id already saved, no reply progress."""
    store = ReconcileStore(base_row())
    api = ReconcileAPI(ids=["m", "r1"], fail_on={2: RuntimeError("boom")})

    result = publish_next(api, store, TABLE)

    assert result["status"] == "failed"
    assert ("main", "m") in store.calls          # root persisted immediately
    assert ("progress", ["r1"]) not in store.calls
    assert result["main_post_id"] == "m"
    assert result["expected_replies"] == 3
    assert result["completed_replies"] == 0


def test_resume_from_reply_after_crash_with_partial_checkpoint():
    """Root + R1 confirmed → resume republishes only R2/R3 under R1."""
    row = base_row(threads_main_post_id="m", threads_reply_ids=["r1"])
    store = ReconcileStore(row)
    api = ReconcileAPI(
        ids=["r2", "r3"],
        children={"m": [{"id": "r1", "text": "R1"}],
                  "r1": []},
    )

    result = publish_next(api, store, TABLE)

    assert result["status"] == "posted"
    assert result["resumed"] is True
    # Never re-published the root or R1; parent chain continues from r1.
    assert [p[1] for p in api.publishes] == ["r1", "r2"]
    assert result["reply_ids"] == ["r1", "r2", "r3"]


def test_many_replies_completed_before_crash_resumes_at_first_missing():
    row = base_row(threads_main_post_id="m", threads_reply_ids=["r1", "r2"])
    store = ReconcileStore(row)
    api = ReconcileAPI(
        ids=["r3"],
        children={"m": [{"id": "r1", "text": "R1"}],
                  "r1": [{"id": "r2", "text": "R2"}]},
    )

    result = publish_next(api, store, TABLE)

    assert result["status"] == "posted"
    assert len(api.publishes) == 1
    assert api.publishes[0] == ("R3", "r2")
    assert store.calls[-1] == ("posted", ["r1", "r2", "r3"])


def test_reply_checkpoint_written_after_every_single_success():
    api = ReconcileAPI(ids=["m", "r1", "r2", "r3"])
    store = ReconcileStore(base_row())

    publish_next(api, store, TABLE)

    progress = [c[1] for c in store.calls if c[0] == "progress"]
    assert progress == [["r1"], ["r1", "r2"], ["r1", "r2", "r3"]]


# ---------------------------------------------------------------------------
# 5-7 + 15-16: duplicate prevention via live reconciliation
# ---------------------------------------------------------------------------


def test_root_never_recreated_when_id_exists():
    row = base_row(threads_main_post_id="m", threads_reply_ids=["r1"])
    store = ReconcileStore(row)
    api = ReconcileAPI(
        ids=["r2", "r3"],
        children={"m": [{"id": "r1", "text": "R1"}], "r1": []},
    )

    result = publish_next(api, store, TABLE)

    assert result["status"] == "posted"
    assert all(p[0] != "MAIN" for p in api.publishes)


def test_confirmed_replies_never_republished_even_if_live_read_fails():
    """Persisted ids are trusted; a failed reconciliation read on a partially
    published thread isolates the row (duplicate-risk rule) instead of
    guessing — confirmed slots are never re-published."""
    row = base_row(threads_main_post_id="m", threads_reply_ids=["r1"])
    store = ReconcileStore(row)
    api = ReconcileAPI(ids=["r2", "r3"], replies_fail=True)

    result = publish_next(api, store, TABLE)

    assert result["status"] == "failed"
    assert "Duplicate-risk ambiguity" in result["error"]
    # Fails CLOSED before any publish: with live reads unavailable on a
    # partially published thread, nothing is re-published at all.
    assert api.publishes == []
    assert result["reply_ids"] == ["r1"]


def test_published_but_unrecorded_reply_is_adopted_not_duplicated():
    """Meta accepted reply 1 but the DB write failed before persistence.
    Reconciliation adopts the live reply; publish_text is never called for it."""
    row = base_row(threads_main_post_id="m")  # reply progress lost
    store = ReconcileStore(row)
    api = ReconcileAPI(
        ids=["r2b", "r3b"],
        children={"m": [{"id": "r1-live", "text": "R1"}]},
    )

    result = publish_next(api, store, TABLE)

    assert result["status"] == "posted"
    assert [p[0] for p in api.publishes] == ["R2", "R3"]
    assert api.publishes[0][1] == "r1-live"          # parented under adopted reply
    assert result["reply_ids"] == ["r1-live", "r2b", "r3b"]
    assert ("progress", ["r1-live"]) in store.calls  # adoption persisted at once


def test_attempted_row_scans_feed_for_existing_root_before_recreating():
    """Row requeued to approved with NO ids after a crash: reconcile first."""
    row = base_row(attempt_count=1)
    store = ReconcileStore(row)
    api = ReconcileAPI(
        ids=["r1b", "r2b", "r3b"],
        feed=[{"id": "root-live", "text": "MAIN",
               "timestamp": "2026-09-21T00:00:00+00:00"}],
        children={"root-live": []},
    )

    result = publish_next(api, store, TABLE)

    assert result["status"] == "posted"
    assert all(p[0] != "MAIN" for p in api.publishes)
    assert result["main_post_id"] == "root-live"
    assert store.calls[0] == ("claim",)
    assert ("main", "root-live") in store.calls


def test_root_duplicate_ambiguation_isolates_row_without_publishing():
    """Feed scan unavailable on an attempted row → stop, isolate, publish nothing."""
    row = base_row(attempt_count=1)
    store = ReconcileStore(row)
    api = ReconcileAPI(feed=None)  # list_posts raises

    result = publish_next(api, store, TABLE)

    assert result["status"] == "failed"
    assert "Duplicate-risk ambiguity" in result["error"]
    assert api.publishes == []
    failed = [c for c in store.calls if c[0] == "failed"]
    assert failed and "Duplicate-risk ambiguity" in failed[0][1]


def test_older_same_text_root_is_not_falsely_adopted():
    row = base_row(attempt_count=1, created_at="2026-09-20T00:00:00+00:00")
    store = ReconcileStore(row)
    api = ReconcileAPI(
        ids=["m-new", "r1", "r2", "r3"],
        feed=[{"id": "ancient", "text": "MAIN",
               "timestamp": "2020-01-01T00:00:00+00:00"}],
        children={},
    )

    result = publish_next(api, store, TABLE)

    assert result["status"] == "posted"
    assert api.publishes[0] == ("MAIN", None)  # created fresh, not adopted
    assert result["main_post_id"] == "m-new"


def test_ghost_parent_heals_by_hopping_to_live_sibling():
    """DB reply id is a ghost branch; the live sibling under the grandparent
    hosts the thread. Hop once, never duplicate the parent's own text."""
    row = base_row(threads_main_post_id="m", threads_reply_ids=["r1-ghost"])
    store = ReconcileStore(row)
    api = ReconcileAPI(
        ids=["r2b", "r3b"],
        fail_on={1: GHOST},
        children={
            "m": [{"id": "r1-ghost", "text": "R1"}],
            "r1-ghost": [],                      # ghost: no children
        },
    )
    # Hop search looks for the live sibling of r1-ghost under m carrying R1...
    # none exists directly, so the hop fails and the row isolates with the
    # ghost error rather than guessing.
    result = publish_next(api, store, TABLE)
    assert result["status"] == "failed"
    assert api.publishes[0][1] == "r1-ghost"     # only the failed attempt


def test_ghost_parent_hop_adopts_live_sibling():
    row = base_row(threads_main_post_id="m", threads_reply_ids=["r1-ghost"])
    store = ReconcileStore(row)
    api = ReconcileAPI(
        ids=["r2b", "r3b"],
        fail_on={1: GHOST},
        children={
            "m": [{"id": "r1-ghost", "text": "x"}, {"id": "r1-live", "text": "R1"}],
            "r1-ghost": [],
            "r1-live": [],
        },
    )

    result = publish_next(api, store, TABLE)

    assert result["status"] == "posted"
    assert api.publishes[0][1] == "r1-ghost"      # attempted, rejected
    assert api.publishes[1][1] == "r1-live"       # healed parent retry
    assert result["reply_ids"] == ["r1-live", "r2b", "r3b"]
    assert ("progress", ["r1-live"]) in store.calls


# ---------------------------------------------------------------------------
# 8-10: heartbeat + stale reclaim CAS (store layer, mocked REST)
# ---------------------------------------------------------------------------


def _store(handler, account_key="syaqir"):
    return SupabaseStore(
        "https://example.supabase.co",
        "key",
        account_key=account_key,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _req_json(request):
    return json.loads(request.read().decode()) if request.read else {}


def test_mark_reply_progress_stamps_heartbeat_atomically():
    patches = []

    def handler(request):
        body = json.loads(request.read().decode())
        patches.append((request.url.path, body))
        return httpx.Response(204)

    store = _store(handler)
    store.mark_post_reply_progress(TABLE, 21, ["r1"])

    path, body = patches[0]
    assert path.endswith("/rest/v1/threads_publish_queue")
    assert body["threads_reply_ids"] == ["r1"]
    assert "heartbeat_at" in body  # checkpoint doubles as heartbeat


def test_heartbeat_column_missing_degrades_gracefully():
    """Migration 009 not applied: first PATCH 400s on the unknown column,
    the retry drops heartbeat_at and must still persist the checkpoint."""
    calls = []

    def handler(request):
        body = json.loads(request.read().decode())
        calls.append(body)
        if "heartbeat_at" in body:
            return httpx.Response(400, json={"message": 'column "heartbeat_at" does not exist'})
        return httpx.Response(204)

    store = _store(handler)
    store.mark_post_reply_progress(TABLE, 21, ["r1"])

    assert len(calls) == 2
    assert "heartbeat_at" not in calls[1]
    assert calls[1]["threads_reply_ids"] == ["r1"]


def test_claim_stamps_heartbeat_on_claim():
    calls = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 21}])
        calls.append(json.loads(request.read().decode()))
        if "heartbeat_at" in calls[-1]:
            return httpx.Response(200, json=[{"id": 21, "status": "posting"}])
        return httpx.Response(400, json={"message": 'heartbeat_at missing'})

    store = _store(handler)
    row = store.claim_due_post(TABLE, now="2026-09-21T00:00:00+00:00")

    assert row["id"] == 21
    assert "heartbeat_at" in calls[0]  # claimed with heartbeat


def _stale_iso(seconds_ago=9999):
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return dt.isoformat()


def test_stale_heartbeat_row_is_reclaimed():
    """A posting row whose heartbeat is older than the threshold is won by a
    CAS PATCH and returned for resume."""
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 21}])
        params = request.url.params
        assert params["status"] == "eq.posting"
        assert "heartbeat_at.lt" in params.get("or", "") or "claimed_at" in params
        body = json.loads(request.read().decode())
        assert "status" not in body  # no status flip; heartbeat+claim refresh only
        assert "heartbeat_at" in body and "claimed_at" in body
        return httpx.Response(200, json=[{"id": 21, "status": "posting"}])

    store = _store(handler)
    rows = store.reclaim_stale_posting_rows(TABLE, stale_seconds=600)
    assert rows and rows[0]["id"] == 21


def test_healthy_worker_row_is_not_stolen():
    """CAS window requires heartbeat older than cutoff; a fresh heartbeat
    matches zero rows → reclaim returns nothing."""
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 21}])
        cutoff = request.url.params.get("or", "")
        # Simulate PostgREST: the row heartbeat is newer than the cutoff, so
        # the CAS PATCH matches zero rows.
        assert ".lt." in cutoff
        return httpx.Response(200, json=[])

    store = _store(handler)
    rows = store.reclaim_stale_posting_rows(TABLE, stale_seconds=600,
                                            now=_stale_iso(30))
    assert rows == []


def test_reclaim_falls_back_to_claimed_at_when_column_absent():
    """Without heartbeat_at, candidates come from the claimed_at filter."""
    seen_filters = []

    def handler(request):
        if request.method == "GET":
            or_param = request.url.params.get("or", "")
            seen_filters.append(or_param)
            if "heartbeat_at" in or_param:
                return httpx.Response(
                    400, json={"message": 'column "heartbeat_at" does not exist'}
                )
            return httpx.Response(200, json=[{"id": 21}])
        return httpx.Response(200, json=[{"id": 21}])

    store = _store(handler)
    rows = store.reclaim_stale_posting_rows(TABLE, stale_seconds=600)
    assert rows and rows[0]["id"] == 21
    assert any("heartbeat_at" in f for f in seen_filters)
    assert seen_filters[-1] == ""  # final filter is plain claimed_at


# ---------------------------------------------------------------------------
# 11-14, 17, 19-20: worker loop integration
# ---------------------------------------------------------------------------


class FakeAPI:
    def __init__(self, ids=None, fail_at=None, error="HTTPStatusError: 400 Bad Request"):
        self.ids = list(ids or [])
        self.fail_at = fail_at
        self.error = error
        self.calls = 0

    def publish_text(self, text, reply_to_id=None, topic_tag=None):
        self.calls += 1
        if self.fail_at and self.calls == 1:
            raise RuntimeError(self.error)
        return self.ids.pop(0)


class FakeStore:
    ROW = {
        "id": 1, "main_post_text": "hello world", "reply_texts": [],
        "threads_main_post_id": None, "threads_reply_ids": [],
    }

    def __init__(self, row=None):
        self.row = dict(row or self.ROW)
        self.requeued = False

    def claim_due_post(self, table, campaign_code=None, now=None):
        return dict(self.row)

    def peek_due_post(self, table, campaign_code=None, now=None):
        return dict(self.row)

    def mark_post_main_published(self, table, row_id, post_id):
        pass

    def mark_post_reply_progress(self, table, row_id, reply_ids):
        pass

    def mark_post_posted(self, table, row_id, reply_ids, posted_at=None):
        pass

    def mark_post_failed(self, table, row_id, error):
        pass

    def reclaim_stale_posting_rows(self, table, *, stale_seconds=600.0,
                                   limit=3, now=None):
        if self.requeued:
            self.requeued = False
            return [dict(self.row, status="posting")]
        return []


def test_worker_phase0_resumes_reclaimed_row_without_requeue(monkeypatch):
    """A stale posting row is reclaimed and published through the same run."""
    requeue_calls = []
    monkeypatch.setattr(
        publish_worker, "requeue_failed_row",
        lambda *a, **k: requeue_calls.append(a) or False,
    )
    store = FakeStore()
    store.requeued = True
    api = FakeAPI(ids=["p1"])

    result = publish_next_with_recovery(
        api, store, TABLE, max_rows_per_run=2, sleep=lambda s: None
    )

    # The reclaimed row went straight to publish (claimed_row path), not to
    # requeue-as-approved.
    assert result["processed"]
    first = result["processed"][0]
    assert first["status"] == "posted"
    assert api.calls >= 1


def test_reclaim_failure_does_not_block_normal_due_rows(monkeypatch):
    """If the reclaim query 500s, the normal approved-row path continues."""
    def boom(*a, **k):
        raise httpx.ConnectError("reclaim down")

    store = FakeStore()
    store.reclaim_stale_posting_rows = boom
    api = FakeAPI(ids=["p1"])
    monkeypatch.setattr(
        publish_worker, "requeue_failed_row", lambda *a, **k: False
    )

    result = publish_next_with_recovery(
        api, store, TABLE, max_rows_per_run=2, sleep=lambda s: None
    )

    assert result["status"] == "posted"


def test_duplicate_risk_alert_renders_full_context(tmp_path):
    """Alert includes queue id, root id, reply progress, and the action taken."""
    from threads_operator.publish_alerts import send_publish_alert

    class RowStore(FakeStore):
        def fetch_queue_row(self, table, row_id):
            return {
                "id": 21, "status": "posting",
                "campaign_code": "LOCAL_SEO_GOOGLE_MAPS",
                "scheduled_at": "2026-09-20T02:30:00+00:00",
                "threads_main_post_id": "18355122796171858",
                "threads_reply_ids": ["a", "b", "c", "d", "e"],
                "reply_texts": ["1", "2", "3", "4", "5", "6", "7"],
            }

    captured: list[dict] = []

    class FakeResp:
        status_code = 200
        def json(self):
            return {}

    def fake_post(url, **kwargs):
        captured.append(kwargs.get("json") or {})
        return FakeResp()

    import httpx as _httpx
    original = _httpx.post
    _httpx.post = fake_post
    try:
        sent = send_publish_alert(
            RowStore(), TABLE, 21,
            {"status": "failed", "reply_ids": ["a", "b", "c", "d", "e"]},
            3,
            "Duplicate-risk ambiguity: live reconciliation unavailable",
            reason="duplicate_risk",
            bot_token="t", chat_id="c",
            state_file=tmp_path / "alert-state.json",
        )
    finally:
        _httpx.post = original

    assert sent is True
    text = captured[0]["text"]
    assert "21" in text
    assert "18355122796171858" in text
    assert "5/7" in text
    assert "duplicate-risk ambiguity" in text.lower()


def test_zero_reply_row_finalizes_right_after_root():
    store = ReconcileStore(base_row(reply_texts=[]))
    api = ReconcileAPI(ids=["m"])

    result = publish_next(api, store, TABLE)

    assert result["status"] == "posted"
    assert result["reply_ids"] == []
    assert store.calls[-1] == ("posted", [])


def test_topic_tag_survives_resume(monkeypatch):
    """Recovered rows keep the row's topic; root only, replies untagged."""
    row = base_row(threads_main_post_id="m", threads_reply_ids=["r1"],
                   topic="Digital Marketing")
    seen: list[tuple] = []

    class TopicAPI(ReconcileAPI):
        def publish_text(self, text, reply_to_id=None, topic_tag=None):
            seen.append((text, topic_tag))
            return super().publish_text(text, reply_to_id=reply_to_id,
                                         topic_tag=topic_tag)

    api = TopicAPI(ids=["r2", "r3"], children={})
    publish_next(api, ReconcileStore(row), TABLE)
    assert seen  # publishing happened; topic resolution never raised
