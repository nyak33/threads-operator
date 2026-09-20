"""Regression tests for the durable publish-queue retry fix.

Covers poisoned-row isolation, persisted retry state across processes,
backoff-aware selection, max-attempt failure, structured Meta classification,
and that existing publish behaviour (success / replies / topic) is unchanged.
"""

from threads_operator.publish_worker import (
    NON_RETRYABLE_META_CODES,
    extract_meta_error,
    is_manual_review_result,
    is_permanent_content_rejection,
    is_transient_publish_result,
    publish_next_with_recovery,
)

TABLE = "threads_publish_queue"


class QueueStore:
    """In-memory account-scoped queue with real backoff-aware selection."""

    def __init__(self, rows):
        # rows: list of dicts; each gets defaults for the retry-state columns.
        self.rows = {}
        for r in rows:
            row = dict(r)
            row.setdefault("status", "approved")
            row.setdefault("reply_texts", [])
            row.setdefault("attempt_count", 0)
            row.setdefault("last_attempt_at", None)
            row.setdefault("next_retry_at", None)
            row.setdefault("scheduled_at", "2026-09-20T00:00:00+00:00")
            row.setdefault("campaign_code", "CAMP")
            self.rows[row["id"]] = row
        self.failed_calls = []
        self.posted_calls = []
        self._headers = {}
        self.client = None
        self.now = "2026-09-20T01:00:00+00:00"

    def _require_account_key(self):
        return "acct"

    # Mirrors supabase_store.requeue_failed_row: only rows currently in a
    # failed/posting state are reset to approved; returns True on success.
    def requeue(self, row_id, error, *, next_retry_at=None, attempt_count=None,
                attempted_at=None):
        row = self.rows.get(int(row_id))
        if row is None or row["status"] not in ("failed", "posting"):
            return False
        row["status"] = "approved"
        row["last_error"] = str(error)
        if attempt_count is not None:
            row["attempt_count"] = int(attempt_count)
        if attempted_at is not None:
            row["last_attempt_at"] = attempted_at
        row["next_retry_at"] = next_retry_at
        return True

    # --- selection -------------------------------------------------------
    def peek_due_post(self, table, campaign_code=None, now=None):
        now = now or self.now
        due = [
            r
            for r in self.rows.values()
            if r["status"] == "approved"
            and r["scheduled_at"] <= now
            and (r["next_retry_at"] is None or r["next_retry_at"] <= now)
        ]
        due.sort(key=lambda r: (r["scheduled_at"], r["id"]))
        return due[0] if due else None

    def claim_due_post(self, table, campaign_code=None, now=None):
        row = self.peek_due_post(table, campaign_code, now)
        if row:
            row["status"] = "posting"
        return row

    def fetch_queue_row(self, table, row_id):
        return self.rows.get(int(row_id))

    # --- mutation --------------------------------------------------------
    def mark_post_main_published(self, table, row_id, post_id):
        self.rows[int(row_id)]["threads_main_post_id"] = post_id

    def mark_post_reply_progress(self, table, row_id, reply_ids):
        self.rows[int(row_id)]["threads_reply_ids"] = list(reply_ids)

    def mark_post_posted(self, table, row_id, reply_ids, posted_at=None):
        row = self.rows[int(row_id)]
        row["status"] = "posted"
        row["threads_reply_ids"] = list(reply_ids)
        row["next_retry_at"] = None
        self.posted_calls.append(int(row_id))

    def mark_post_failed(self, table, row_id, error, *, clear_next_retry=True):
        row = self.rows[int(row_id)]
        row["status"] = "failed"
        row["last_error"] = str(error)
        if clear_next_retry:
            row["next_retry_at"] = None
        self.failed_calls.append((int(row_id), str(error)))

    def record_publish_attempt(self, table, row_id, *, attempt_count,
                               attempted_at=None, next_retry_at=None, last_error=None):
        row = self.rows[int(row_id)]
        row["attempt_count"] = int(attempt_count)
        row["last_attempt_at"] = attempted_at
        row["next_retry_at"] = next_retry_at


class API:
    """publish_text stub; per-row behaviour via `script`."""

    def __init__(self, script=None):
        # script: callable(call_count, text) -> id or raises
        self.script = script or (lambda n, text: f"post-{n}")
        self.calls = []

    def publish_text(self, text, reply_to_id=None, topic_tag=None):
        self.calls.append((text, reply_to_id, topic_tag))
        return self.script(len(self.calls), text)


def _row(id_, text="hi", **kw):
    return {"id": id_, "main_post_text": text, **kw}


# --- 1. oldest due row transiently fails once -> requeued with backoff, then
#        succeeds on a later tick (after backoff elapses) -------------------
def test_transient_once_then_recovers_next_tick(monkeypatch):
    store = QueueStore([_row(1)])
    state = {"n": 0}

    def script(n, text):
        if n == 1:
            raise RuntimeError("HTTPStatusError: 400 Bad Request")
        return "p1"

    api = API(script)
    monkeypatch.setattr(
        "threads_operator.publish_worker.requeue_failed_row",
        lambda st, table, rid, err, **k: st.requeue(rid, err, **k),
    )
    # First tick: transient failure -> requeued with a backoff gate, not posted.
    r1 = publish_next_with_recovery(api, store, TABLE, sleep=lambda s: None)
    assert r1["requeued"] is True
    assert store.rows[1]["next_retry_at"] is not None
    assert store.rows[1]["status"] == "approved"

    # Later tick after backoff elapses -> row eligible again -> posts.
    store.rows[1]["next_retry_at"] = None
    r2 = publish_next_with_recovery(api, store, TABLE, sleep=lambda s: None)
    assert r2["status"] == "posted"
    assert r2["main_post_id"] == "p1"


# --- 2. retry state persists across separate executor processes ------------
def test_retry_state_persists_across_processes(monkeypatch):
    store = QueueStore([_row(1)])
    monkeypatch.setattr(
        "threads_operator.publish_worker.requeue_failed_row",
        lambda st, table, rid, err, **k: st.requeue(rid, err, **k),
    )

    def fail(n, text):
        raise RuntimeError("HTTPStatusError: 400 Bad Request")

    # First process: fails, persists attempt_count=1.
    r1 = publish_next_with_recovery(API(fail), store, TABLE, max_attempts=1)
    assert store.rows[1]["attempt_count"] == 1
    assert store.rows[1]["next_retry_at"] is not None

    # Simulate backoff elapsed -> eligible again; second process reads count=1.
    store.rows[1]["next_retry_at"] = None
    r2 = publish_next_with_recovery(API(fail), store, TABLE, max_attempts=1)
    assert r2["persisted_attempts"] == 2


# --- 3. row in backoff does not block a later eligible due row -------------
def test_backoff_row_does_not_block_later():
    store = QueueStore([
        _row(1, scheduled_at="2026-09-20T00:00:00+00:00",
             next_retry_at="2026-09-20T05:00:00+00:00"),  # in backoff
        _row(2, scheduled_at="2026-09-20T00:30:00+00:00"),
    ])
    api = API()
    res = publish_next_with_recovery(api, store, TABLE)
    assert res["status"] == "posted"
    assert 2 in store.posted_calls
    assert 1 not in store.posted_calls


# --- 4. row reaches max attempts -> marked failed --------------------------
def test_max_attempts_marks_failed():
    store = QueueStore([_row(1, attempt_count=4)])  # 5th attempt hits cap
    alerts = []

    def fail(n, text):
        raise RuntimeError("HTTPStatusError: 400 Bad Request")

    res = publish_next_with_recovery(
        API(fail), store, TABLE, max_persisted_attempts=5,
        alert=lambda *a, **k: alerts.append(a),
    )
    assert res["max_attempts_reached"] is True
    assert store.rows[1]["status"] == "failed"
    assert alerts, "expected a max-attempts alert"


# --- 5. failed poisoned row does not block later rows ----------------------
def test_failed_poison_does_not_block():
    store = QueueStore([
        _row(1, status="failed"),
        _row(2, scheduled_at="2026-09-20T00:30:00+00:00"),
    ])
    res = publish_next_with_recovery(API(), store, TABLE)
    assert res["status"] == "posted"
    assert 2 in store.posted_calls


# --- 6. code 24 / subcode 4279009 does not auto-requeue --------------------
def test_code_24_subcode_4279009_no_requeue():
    err = (
        'HTTPStatusError: Meta error: {"error":{"message":"resource does not exist",'
        '"type":"OAuthException","code":24,"error_subcode":4279009}}'
    )
    assert is_permanent_content_rejection({"error": err})
    assert is_manual_review_result({"error": err})
    assert not is_transient_publish_result({"error": err})

    store = QueueStore([_row(1)])

    def fail(n, text):
        raise RuntimeError(err)

    res = publish_next_with_recovery(API(fail), store, TABLE)
    assert res.get("manual_review") is True
    assert store.rows[1]["status"] == "failed"
    assert "requeued" not in res or not res["requeued"]


# --- 6b. over-length text (500) is a permanent rejection -------------------
def test_overlength_text_is_permanent():
    err = (
        '{"error":{"message":"The text field cannot exceed 500 characters.",'
        '"type":"OAuthException","code":-1}} 500 Server Error'
    )
    assert is_permanent_content_rejection({"error": err})


# --- structured extraction preserves fields --------------------------------
def test_extract_meta_error_fields():
    err = (
        'Meta error: {"error":{"message":"x","type":"OAuthException",'
        '"code":24,"error_subcode":4279009}}'
    )
    meta = extract_meta_error({"error": "500 " + err})
    assert meta["code"] == 24
    assert meta["subcode"] == 4279009
    assert (24, 4279009) in NON_RETRYABLE_META_CODES


# --- 7. successful row resets/completes state ------------------------------
def test_success_clears_backoff():
    store = QueueStore([_row(1, attempt_count=2,
                             next_retry_at="2026-09-20T00:00:00+00:00")])
    res = publish_next_with_recovery(API(), store, TABLE)
    assert res["status"] == "posted"
    assert store.rows[1]["next_retry_at"] is None
    assert store.rows[1]["status"] == "posted"


# --- 8. claim/lock safety: claimed row not re-claimed ----------------------
def test_claim_lock_safe():
    store = QueueStore([_row(1)])
    # After claim, status becomes 'posting'; a second peek must not return it.
    first = store.claim_due_post(TABLE)
    assert first["id"] == 1
    assert store.peek_due_post(TABLE) is None


# --- 9. normal publishing still works --------------------------------------
def test_normal_publish_unchanged():
    store = QueueStore([_row(1)])
    api = API()
    res = publish_next_with_recovery(api, store, TABLE)
    assert res["status"] == "posted"
    assert store.rows[1]["status"] == "posted"


# --- 10. replies still publish ---------------------------------------------
def test_replies_publish():
    store = QueueStore([_row(1, reply_texts=["r1", "r2"])])
    api = API()
    res = publish_next_with_recovery(api, store, TABLE)
    assert res["status"] == "posted"
    assert store.rows[1]["threads_reply_ids"]


# --- 11. topic/topic_tag passed through ------------------------------------
def test_topic_passthrough():
    store = QueueStore([_row(1, topic="Digital Marketing")])
    captured = {}

    def script(n, text):
        return "p-main"

    api = API(script)
    publish_next_with_recovery(api, store, TABLE)
    # topic_tag is forwarded to publish_text as keyword arg.
    assert api.calls and api.calls[0][2] == "Digital Marketing"


# --- bounded backlog: multiple due rows processed in one run ---------------
def test_bounded_backlog_processes_multiple():
    store = QueueStore([_row(1), _row(2), _row(3), _row(4)])
    res = publish_next_with_recovery(API(), store, TABLE, max_rows_per_run=3)
    posted = [r for r in res["processed"] if r.get("status") == "posted"]
    assert len(posted) == 3  # bounded at max_rows_per_run
    assert store.rows[4]["status"] == "approved"  # 4th left for next run
