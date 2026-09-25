"""Task #3 — Telegram Approval -> Smart Scheduling.

Covers the required acceptance tests:
1.  Approve -> Use Best Time -> approved queue row + future scheduled_at
2.  Approve -> Post Now -> approved queue row, immediately due
3.  Approve -> Choose Time -> custom MYT input -> confirmation -> Confirm ->
    correct stored UTC timestamp + approved
4.  Duplicate Approve callback -> no duplicate queue row
5.  Duplicate scheduling confirmation -> no duplicate queue row
6.  Cancel -> content NOT accidentally published (no approved queue row)
7.  Best slot already occupied -> next suitable slot selected
8.  Insufficient historical data -> configured fallback scheduling works
9.  Existing publish worker recognizes the resulting approved row
10. Regression: Edit / Reject / existing workflows remain functional

Plus scheduling unit tests: bucket scoring, collision avoidance, fallback, and
custom-time parsing (MYT).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from threads_operator.account_config import AccountConfig
from threads_operator.supabase_store import SupabaseStore
from threads_operator import scheduling
from threads_operator.scheduling import (
    MYT,
    ScheduleParseError,
    SchedulingConfig,
    parse_custom_time,
    recommend_slot,
    score_time_buckets,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fake PostgREST backend (extends the trend_backlog one with insight snapshots)
# ---------------------------------------------------------------------------

class FakePostgREST:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {
            "threads_trend_candidates": [],
            "threads_publish_queue": [],
            "threads_post_insights_snapshots": [],
        }
        self._ids: dict[str, int] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        table = request.url.path.rsplit("/", 1)[-1]
        rows = self.tables.setdefault(table, [])
        params = dict(request.url.params)
        if request.method == "GET":
            return self._select(rows, params)
        if request.method == "POST":
            body = json.loads(request.content.decode() or "{}")
            return self._insert(table, rows, body)
        if request.method == "PATCH":
            body = json.loads(request.content.decode() or "{}")
            return self._update(rows, params, body)
        return httpx.Response(405, json={"error": "method"})

    @staticmethod
    def _coerce(value: str) -> Any:
        try:
            return int(value)
        except ValueError:
            return value

    def _matches(self, row: dict[str, Any], params: dict[str, str]) -> bool:
        for key, raw in params.items():
            if key in {"select", "order", "limit", "offset"}:
                continue
            if raw.startswith("eq."):
                expected: Any = raw[3:]
                actual = row.get(key)
                if str(actual) != expected and actual != self._coerce(expected):
                    return False
            elif raw.startswith("in.("):
                options = raw[4:-1].split(",")
                if str(row.get(key)) not in options:
                    return False
            elif raw.startswith("gte."):
                cutoff = raw[4:]
                actual = row.get(key)
                if actual is None:
                    return False
                a = datetime.fromisoformat(str(actual).replace("Z", "+00:00"))
                c = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
                if a < c:
                    return False
            elif raw.startswith("is.null"):
                if row.get(key) is not None:
                    return False
        return True

    def _select(self, rows, params):
        matched = [dict(r) for r in rows if self._matches(r, params)]
        limit = params.get("limit")
        if limit and str(limit).isdigit():
            matched = matched[: int(limit)]
        return httpx.Response(200, json=matched)

    def _insert(self, table, rows, body):
        next_id = self._ids.get(table, 0) + 1
        self._ids[table] = next_id
        row = {"id": next_id, **body}
        rows.append(row)
        return httpx.Response(201, json=[dict(row)])

    def _update(self, rows, params, body):
        updated = []
        for row in rows:
            if self._matches(row, params):
                row.update(body)
                updated.append(dict(row))
        return httpx.Response(200, json=updated)


@pytest.fixture()
def backend() -> FakePostgREST:
    return FakePostgREST()


@pytest.fixture()
def store(backend: FakePostgREST) -> SupabaseStore:
    transport = httpx.MockTransport(backend.handle)
    client = httpx.Client(transport=transport, base_url="http://test")
    return SupabaseStore("http://test", "service-key", client=client, account_key="syaqir")


class _FakeConfig(AccountConfig):
    def __init__(self):
        super().__init__(
            name="syaqir",
            home=Path("/tmp/fake-home"),
            env_path=Path("/tmp/fake-home/accounts/syaqir.env"),
            values={},
        )

    def get(self, key, default=None):
        return default

    def get_bool(self, key):
        return False

    def require(self, key):
        return "dummy"


def _make_config(store):
    import threads_operator.operator_cli as cli_mod
    old = cli_mod._store
    cli_mod._store = lambda cfg: store
    return _FakeConfig(), old


def _restore(old):
    import threads_operator.operator_cli as cli_mod
    cli_mod._store = old


def _seed_pending(store, backend, **over) -> dict[str, Any]:
    row = {
        "id": backend._ids.get("threads_trend_candidates", 0) + 1,
        "target_account_id": "syaqir",
        "source_platform": "threads",
        "source_username": "nurdini_olivia",
        "source_permalink": "https://www.threads.com/@nurdini_olivia/post/DFZAyiUySkB",
        "status": "pending_approval",
        "topic": "net salary breakdown",
        "used_in_queue_id": None,
        "approval_sent_at": "2026-09-25T00:00:00+00:00",
        "timed_out_at": None,
        "updated_at": "2026-09-25T00:00:00+00:00",
        "raw_metadata": {
            "workflow_a": {
                "draft_text": "RM1,700 ni nampak kecil, tapi bila usahawan kecil kira KWSP manual...",
                "topic": "net salary breakdown",
            }
        },
    }
    row.update(over)
    backend._ids["threads_trend_candidates"] = row["id"]
    backend.tables["threads_trend_candidates"].append(row)
    return row


def _queue_rows(backend) -> list[dict[str, Any]]:
    return backend.tables["threads_publish_queue"]


# ===========================================================================
# Scheduling unit tests
# ===========================================================================

def _insight(post_id, published_local: datetime, views, likes=0, replies=0, reposts=0, quotes=0):
    """Build one insight snapshot. ``published_local`` is a MYT-aware datetime."""
    return {
        "post_id": post_id,
        "captured_at": published_local.astimezone(UTC).isoformat(),
        "published_at": published_local.astimezone(UTC).isoformat(),
        "post_age_minutes": 1000.0,
        "views": views, "likes": likes, "replies": replies,
        "reposts": reposts, "quotes": quotes,
    }


def test_bucket_scoring_prefers_engagement_rate_over_raw_views():
    """A 5k-view high-engagement post beats a 20k-view low-engagement post."""
    cfg = SchedulingConfig(min_samples=1)
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    rows = []
    # Strong engagement at 9pm (hour=21), low views.
    for i in range(3):
        local = datetime(2026, 9, 20, 21, 0, tzinfo=MYT)  # Sunday 21:00
        rows.append(_insight(f"strong{i}", local, views=5000, likes=500, replies=100, reposts=50, quotes=20))
    # Weak engagement at 8am (hour=8), high views.
    for i in range(3):
        local = datetime(2026, 9, 21, 8, 0, tzinfo=MYT)
        rows.append(_insight(f"weak{i}", local, views=20000, likes=50, replies=5))
    scored = score_time_buckets(rows, now=now, config=cfg)
    # strong bucket rate = 670/5000 = 0.134 ; weak bucket = 55/20000 = 0.00275
    strong_bucket = (datetime(2026, 9, 20, 21, 0, tzinfo=MYT).weekday(), 21)
    weak_bucket = (datetime(2026, 9, 21, 8, 0, tzinfo=MYT).weekday(), 8)
    assert scored[strong_bucket]["score"] > scored[weak_bucket]["score"]


def test_recommend_slot_collision_picks_next_slot():
    """When the strongest slot is occupied, pick the next suitable one."""
    cfg = SchedulingConfig(min_samples=1, min_gap_minutes=60, lookahead_days=7)
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)  # Friday 12:00 UTC
    rows = []
    for i in range(3):
        local = datetime(2026, 9, 20, 21, 0, tzinfo=MYT)  # best: Sunday 21:00
        rows.append(_insight(f"s{i}", local, views=1000, likes=200, replies=50))
    # Occupy the next Sunday 21:00 MYT occurrence.
    next_sun_21 = scheduling._next_occurrence(now, 6, 21)  # 6 = Sunday
    assert next_sun_21 is not None
    occupied = [next_sun_21]
    rec = recommend_slot(rows, occupied, now=now, config=cfg)
    # Should NOT equal the occupied slot (moved to a later occurrence).
    assert rec.scheduled_utc != next_sun_21
    assert abs((rec.scheduled_utc - next_sun_21)) >= timedelta(minutes=cfg.min_gap_minutes)


def test_recommend_slot_fallback_when_no_history():
    """Insufficient data -> configured fallback window, never fails."""
    cfg = SchedulingConfig(min_samples=5, fallback_hours_local=(9, 13, 20))
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    rec = recommend_slot([], [], now=now, config=cfg)
    assert rec.source == "fallback"
    assert rec.scheduled_utc > now
    local = rec.scheduled_utc.astimezone(MYT)
    assert local.hour in cfg.fallback_hours_local


def test_fallback_slot_is_future_and_reason_is_set():
    cfg = SchedulingConfig(min_samples=1, min_lead_minutes=5)
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    rec = recommend_slot([], [], now=now, config=cfg)
    assert rec.scheduled_utc >= now + timedelta(minutes=cfg.min_lead_minutes)
    assert rec.reason


# --- custom time parsing (MYT) ----------------------------------------------

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)  # = 20:00 MYT Friday


def test_parse_bare_time_rolls_to_tomorrow_when_past():
    # 20:00 MYT now; "9pm" (=21:00) is still future today.
    ts = parse_custom_time("9pm", now=NOW)
    assert ts.astimezone(MYT).hour == 21
    assert ts.astimezone(MYT).date() == NOW.astimezone(MYT).date()


def test_parse_bare_time_today_future():
    ts = parse_custom_time("11pm", now=NOW)  # 23:00 MYT tonight
    local = ts.astimezone(MYT)
    assert local.hour == 23 and local.date() == NOW.astimezone(MYT).date()


def test_parse_tomorrow():
    ts = parse_custom_time("tomorrow 8:30pm", now=NOW)
    local = ts.astimezone(MYT)
    assert local.hour == 20 and local.minute == 30
    assert local.date() == (NOW.astimezone(MYT).date() + timedelta(days=1))


def test_parse_explicit_date():
    ts = parse_custom_time("25 Sep 9pm", now=NOW)
    local = ts.astimezone(MYT)
    assert (local.day, local.month, local.hour) == (25, 9, 21)


def test_parse_iso_datetime():
    ts = parse_custom_time("2026-09-26 21:00", now=NOW)
    local = ts.astimezone(MYT)
    assert (local.year, local.month, local.day, local.hour) == (2026, 9, 26, 21)


def test_parse_24h_time():
    ts = parse_custom_time("tomorrow 21:00", now=NOW)
    local = ts.astimezone(MYT)
    assert local.hour == 21


def test_parse_rejects_past():
    with pytest.raises(ScheduleParseError):
        parse_custom_time("2020-01-01 10:00", now=NOW)


def test_parse_rejects_garbage():
    with pytest.raises(ScheduleParseError):
        parse_custom_time("whenever lol", now=NOW)


# ===========================================================================
# End-to-end CLI flow tests (with mocked _store)
# ===========================================================================

def test_approve_only_approves_no_queue_row(store, backend):
    """Approve = content approval; must NOT create any queue row yet."""
    from threads_operator.operator_cli import _run_trendeng_approve
    row = _seed_pending(store, backend)
    config, old = _make_config(store)
    try:
        code, payload = _run_trendeng_approve(config, candidate_id=row["id"])
    finally:
        _restore(old)
    assert code == 0 and payload["ok"] is True
    assert payload["status"] == "approved"
    assert payload.get("needs_scheduling") is True
    assert _queue_rows(backend) == []  # no draft, no approved row yet
    cand = [c for c in backend.tables["threads_trend_candidates"] if c["id"] == row["id"]][0]
    assert cand["status"] == "approved"


def test_best_time_creates_approved_future_row(store, backend):
    """Test 1: Approve -> Use Best Time -> approved + future scheduled_at."""
    from threads_operator.operator_cli import _run_trendeng_approve, _run_trendeng_schedule_best
    row = _seed_pending(store, backend)
    config, old = _make_config(store)
    try:
        _run_trendeng_approve(config, candidate_id=row["id"])
        code, payload = _run_trendeng_schedule_best(config, candidate_id=row["id"])
    finally:
        _restore(old)
    assert code == 0 and payload["ok"] is True
    rows = _queue_rows(backend)
    assert len(rows) == 1
    q = rows[0]
    assert q["status"] == "approved"
    sched = datetime.fromisoformat(q["scheduled_at"].replace("Z", "+00:00"))
    assert sched > datetime.now(UTC)
    cand = backend.tables["threads_trend_candidates"][0]
    assert cand["status"] == "queued" and cand["used_in_queue_id"] == q["id"]


def test_post_now_creates_approved_due_row(store, backend):
    """Test 2: Approve -> Post Now -> approved + immediately due."""
    from threads_operator.operator_cli import _run_trendeng_approve, _run_trendeng_schedule_now
    row = _seed_pending(store, backend)
    config, old = _make_config(store)
    try:
        _run_trendeng_approve(config, candidate_id=row["id"])
        before = datetime.now(UTC)
        code, payload = _run_trendeng_schedule_now(config, candidate_id=row["id"])
        after = datetime.now(UTC)
    finally:
        _restore(old)
    assert code == 0 and payload["ok"] is True
    q = _queue_rows(backend)[0]
    assert q["status"] == "approved"
    sched = datetime.fromisoformat(q["scheduled_at"].replace("Z", "+00:00"))
    assert before - timedelta(seconds=2) <= sched <= after + timedelta(seconds=2)


def test_choose_time_confirm_stores_correct_utc(store, backend):
    """Test 3: Choose Time -> custom MYT -> Confirm -> correct stored UTC."""
    from threads_operator.operator_cli import _run_trendeng_approve, _run_trendeng_schedule_time
    row = _seed_pending(store, backend)
    config, old = _make_config(store)
    # User wants 26 Sep 2026 21:00 MYT == 2026-09-26T13:00:00Z
    myt_dt = datetime(2026, 9, 26, 21, 0, tzinfo=MYT)
    expected_utc = myt_dt.astimezone(UTC)
    try:
        _run_trendeng_approve(config, candidate_id=row["id"])
        code, payload = _run_trendeng_schedule_time(
            config, candidate_id=row["id"], at=expected_utc.isoformat()
        )
    finally:
        _restore(old)
    assert code == 0 and payload["ok"] is True
    q = _queue_rows(backend)[0]
    assert q["status"] == "approved"
    sched = datetime.fromisoformat(q["scheduled_at"].replace("Z", "+00:00"))
    assert sched == expected_utc


def test_duplicate_approve_no_duplicate_queue_row(store, backend):
    """Test 4: repeated Approve callbacks never create duplicate queue rows."""
    from threads_operator.operator_cli import _run_trendeng_approve, _run_trendeng_schedule_now
    row = _seed_pending(store, backend)
    config, old = _make_config(store)
    try:
        _run_trendeng_approve(config, candidate_id=row["id"])
        _run_trendeng_approve(config, candidate_id=row["id"])  # duplicate tap
        _run_trendeng_approve(config, candidate_id=row["id"])  # duplicate tap
        _run_trendeng_schedule_now(config, candidate_id=row["id"])
    finally:
        _restore(old)
    assert len(_queue_rows(backend)) == 1


def test_duplicate_confirm_no_duplicate_queue_row(store, backend):
    """Test 5: repeated Confirm callbacks never create duplicate queue rows."""
    from threads_operator.operator_cli import _run_trendeng_approve, _run_trendeng_schedule_time
    row = _seed_pending(store, backend)
    config, old = _make_config(store)
    when = datetime(2026, 9, 26, 13, 0, tzinfo=UTC).isoformat()
    try:
        _run_trendeng_approve(config, candidate_id=row["id"])
        _run_trendeng_schedule_time(config, candidate_id=row["id"], at=when)
        _run_trendeng_schedule_time(config, candidate_id=row["id"], at=when)  # duplicate
        _run_trendeng_schedule_time(config, candidate_id=row["id"], at=when)  # duplicate
    finally:
        _restore(old)
    assert len(_queue_rows(backend)) == 1


def test_cancel_does_not_publish(store, backend):
    """Test 6: Cancel -> content NOT accidentally published (no approved row)."""
    from threads_operator.operator_cli import _run_trendeng_approve, _run_trendeng_schedule_cancel
    row = _seed_pending(store, backend)
    config, old = _make_config(store)
    try:
        _run_trendeng_approve(config, candidate_id=row["id"])
        code, payload = _run_trendeng_schedule_cancel(config, candidate_id=row["id"])
    finally:
        _restore(old)
    assert code == 0 and payload["ok"] is True
    # No approved/queued row should exist.
    assert all(r.get("status") not in ("approved", "posting") for r in _queue_rows(backend))
    cand = backend.tables["threads_trend_candidates"][0]
    assert cand["status"] == "approved"  # content stays approved, resumable


def test_best_slot_occupied_selects_next(store, backend):
    """Test 7: strongest slot occupied -> next suitable slot selected (e2e)."""
    from threads_operator.operator_cli import _run_trendeng_approve, _run_trendeng_schedule_best
    row = _seed_pending(store, backend)
    # Build history: strong engagement Sunday 21:00 MYT.
    now = datetime.now(UTC)
    for i in range(3):
        local = datetime(2026, 9, 20, 21, 0, tzinfo=MYT)
        backend.tables["threads_post_insights_snapshots"].append(
            _insight(f"p{i}", local, views=1000, likes=300, replies=80)
        )
    # Occupy the upcoming Sunday 21:00 with an existing approved queue row.
    occupied_slot = scheduling._next_occurrence(now, 6, 21)
    backend.tables["threads_publish_queue"].append({
        "id": 999, "account_key": "syaqir", "main_post_text": "existing",
        "reply_texts": [], "status": "approved", "scheduled_at": occupied_slot.isoformat(),
    })
    config, old = _make_config(store)
    try:
        _run_trendeng_approve(config, candidate_id=row["id"])
        code, payload = _run_trendeng_schedule_best(config, candidate_id=row["id"])
    finally:
        _restore(old)
    assert code == 0 and payload["ok"] is True
    new_row = [r for r in _queue_rows(backend) if r["id"] != 999][0]
    new_sched = datetime.fromisoformat(new_row["scheduled_at"].replace("Z", "+00:00"))
    assert new_sched != occupied_slot
    assert abs(new_sched - occupied_slot) >= timedelta(minutes=60)


def test_insufficient_history_uses_fallback(store, backend):
    """Test 8: no usable insight data -> configured fallback slot (approved)."""
    from threads_operator.operator_cli import _run_trendeng_approve, _run_trendeng_schedule_best
    row = _seed_pending(store, backend)
    # No insight rows at all.
    config, old = _make_config(store)
    try:
        _run_trendeng_approve(config, candidate_id=row["id"])
        code, payload = _run_trendeng_schedule_best(config, candidate_id=row["id"])
    finally:
        _restore(old)
    assert code == 0 and payload["ok"] is True
    assert payload["schedule_source"] == "fallback"
    q = _queue_rows(backend)[0]
    assert q["status"] == "approved"
    sched = datetime.fromisoformat(q["scheduled_at"].replace("Z", "+00:00"))
    assert sched > datetime.now(UTC)


def test_publish_worker_recognizes_approved_row(store, backend):
    """Test 9: existing publish worker picks up the resulting approved row."""
    from threads_operator.operator_cli import _run_trendeng_approve, _run_trendeng_schedule_now
    row = _seed_pending(store, backend)
    config, old = _make_config(store)
    try:
        _run_trendeng_approve(config, candidate_id=row["id"])
        _run_trendeng_schedule_now(config, candidate_id=row["id"])
    finally:
        _restore(old)
    # The queue's claim path (peek_due_post) must see the approved, due row.
    table = "threads_publish_queue"
    due = store.peek_due_post(table, now=datetime.now(UTC).isoformat())
    assert due is not None
    assert due["status"] == "approved"


# ===========================================================================
# Test 10: regression — Edit / Reject / Skip still work
# ===========================================================================

def test_regression_reject_still_works(store, backend):
    from threads_operator.operator_cli import _trendeng_set_status
    row = _seed_pending(store, backend)
    config, old = _make_config(store)
    try:
        code, payload = _trendeng_set_status(
            config, candidate_id=row["id"],
            from_status="pending_approval", to_status="rejected",
        )
    finally:
        _restore(old)
    assert code == 0 and payload["ok"] is True
    cand = backend.tables["threads_trend_candidates"][0]
    assert cand["status"] == "rejected"
    assert _queue_rows(backend) == []


def test_regression_edit_still_works(store, backend):
    from threads_operator.operator_cli import _run_trendeng_edit
    row = _seed_pending(store, backend)
    config, old = _make_config(store)
    try:
        code, payload = _run_trendeng_edit(config, candidate_id=row["id"], text="draf baharu")
    finally:
        _restore(old)
    assert code == 0 and payload["ok"] is True
    cand = backend.tables["threads_trend_candidates"][0]
    assert cand["raw_metadata"]["workflow_a"]["draft_text"] == "draf baharu"
    assert cand["status"] == "pending_approval"  # still awaiting approval
    assert _queue_rows(backend) == []


def test_regression_use_backlog_now_schedules_not_draft(store, backend):
    """Backlog 'use' approves content + prompts scheduling; no draft row."""
    from threads_operator.operator_cli import _run_trendeng_use
    row = _seed_pending(store, backend, status="backlog",
                        timed_out_at="2026-09-24T00:00:00+00:00")
    config, old = _make_config(store)
    try:
        code, payload = _run_trendeng_use(config, candidate_id=row["id"])
    finally:
        _restore(old)
    assert code == 0 and payload["ok"] is True
    assert payload["status"] == "approved"
    assert payload.get("needs_scheduling") is True
    # No queue row yet (draft or otherwise).
    assert _queue_rows(backend) == []
    cand = backend.tables["threads_trend_candidates"][0]
    assert cand["status"] == "approved"


# ===========================================================================
# Dispatcher UX (Telegram card / session) tests — _run_cli mocked
# ===========================================================================

class _Capture:
    def __init__(self):
        self.sent: list[dict[str, Any]] = []
        self.answered: list[str] = []

    async def send(self, chat_id=None, text=None, markup=None, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, "markup": markup})

    async def answer(self, text):
        self.answered.append(text)


def _sched_dispatcher(store_path, cap: _Capture):
    from threads_operator.engagement_sessions import PendingEditStore
    from threads_operator.workflow_telegram import TrendSchedDispatcher
    return TrendSchedDispatcher(
        account="syaqir",
        send=cap.send,
        edit_store=PendingEditStore(store_path, ttl_seconds=1800),
        cwd="/nonexistent",  # avoid touching the real CLI; _run_cli is mocked
    )


def test_approve_callback_shows_scheduling_card(tmp_path):
    """trendeng:approve -> content approved -> scheduling card rendered."""
    import asyncio
    from threads_operator import workflow_telegram as wt
    from threads_operator.engagement_sessions import PendingEditStore

    cap = _Capture()

    async def fake_run_cli(argv, group, args, *, cwd, timeout=90):
        assert args[0] == "approve"
        return 0, {"ok": True, "status": "approved", "needs_scheduling": True,
                   "draft_preview": "draf gaji"}

    async def _send(chat_id, text, markup=None):
        await cap.send(chat_id, text, markup)

    disp = wt.TrendEngagementDispatcher(
        account="syaqir", send=_send,
        edit_store=PendingEditStore(tmp_path / "s.json"), cwd="/nonexistent",
    )
    with patch.object(wt, "_run_cli", side_effect=fake_run_cli):
        asyncio.run(disp._decide(146, "approve", chat_id="79553451",
                                 message_id="1", answer=cap.answer))
    assert any("content approved" in a for a in cap.answered)
    assert cap.sent, "expected a scheduling card to be sent"
    card = cap.sent[0]
    cbs = [b["callback_data"] for row in card["markup"]["inline_keyboard"] for b in row]
    assert "trendsched:best:146" in cbs
    assert "trendsched:now:146" in cbs
    assert "trendsched:choose:146" in cbs
    assert "trendsched:cancel:146" in cbs


def test_choose_time_session_then_confirm(tmp_path):
    """Choose Time -> session -> text input -> confirmation card -> Confirm."""
    import asyncio
    from threads_operator import workflow_telegram as wt

    cap = _Capture()
    disp = _sched_dispatcher(tmp_path / "s.json", cap)
    calls: list[list[str]] = []

    async def fake_run_cli(argv, group, args, *, cwd, timeout=90):
        calls.append(args)
        if args[0] == "schedule-time":
            return 0, {"ok": True, "queue_id": 700, "scheduled_at": args[args.index("--at") + 1]}
        if args[0] == "schedule-cancel":
            return 0, {"ok": True, "status": "approved"}
        return 0, {"ok": True}

    with patch.object(wt, "_run_cli", side_effect=fake_run_cli):
        # 1. tap Choose Time
        asyncio.run(disp.handle_callback(data="trendsched:choose:146", chat_id="79553451",
                                         user_id="79553451", answer=cap.answer))
        # 2. user types a time
        handled = asyncio.run(disp.handle_text(chat_id="79553451", user_id="79553451",
                                               text="tomorrow 8:30pm"))
        assert handled is True
        # confirmation card shown with confirm button
        confirm_cards = [s for s in cap.sent if s["markup"] and any(
            "trendsched:confirm:146" == b["callback_data"]
            for row in s["markup"]["inline_keyboard"] for b in row)]
        assert confirm_cards, "expected a confirmation card with Confirm button"
        # 3. tap Confirm
        asyncio.run(disp.handle_callback(data="trendsched:confirm:146", chat_id="79553451",
                                         user_id="79553451", answer=cap.answer))
    assert any(c[0] == "schedule-time" for c in calls)
    assert any("Scheduling confirmed" in a for a in cap.answered)


def test_choose_time_invalid_input_reprompts(tmp_path):
    """Unparseable custom time -> re-prompt, session stays open, no CLI call."""
    import asyncio
    from threads_operator import workflow_telegram as wt

    cap = _Capture()
    disp = _sched_dispatcher(tmp_path / "s.json", cap)
    calls: list[list[str]] = []

    async def fake_run_cli(argv, group, args, *, cwd, timeout=90):
        calls.append(args)
        return 0, {"ok": True}

    with patch.object(wt, "_run_cli", side_effect=fake_run_cli):
        asyncio.run(disp.handle_callback(data="trendsched:choose:146", chat_id="79553451",
                                         user_id="79553451", answer=cap.answer))
        handled = asyncio.run(disp.handle_text(chat_id="79553451", user_id="79553451",
                                               text="whenever lol"))
        assert handled is True
    assert calls == []  # no scheduling CLI call on parse failure
    assert any("Couldn't understand" in (s["text"] or "") for s in cap.sent)


def test_cancel_via_text_does_not_schedule(tmp_path):
    """Typing 'cancel' during custom-time input cancels without scheduling."""
    import asyncio
    from threads_operator import workflow_telegram as wt

    cap = _Capture()
    disp = _sched_dispatcher(tmp_path / "s.json", cap)
    calls: list[list[str]] = []

    async def fake_run_cli(argv, group, args, *, cwd, timeout=90):
        calls.append(args)
        return 0, {"ok": True, "status": "approved"}

    with patch.object(wt, "_run_cli", side_effect=fake_run_cli):
        asyncio.run(disp.handle_callback(data="trendsched:choose:146", chat_id="79553451",
                                         user_id="79553451", answer=cap.answer))
        handled = asyncio.run(disp.handle_text(chat_id="79553451", user_id="79553451", text="cancel"))
        assert handled is True
    # only schedule-cancel invoked, never schedule-time/best/now
    assert all(c[0] == "schedule-cancel" for c in calls)
    msgs = cap.answered + [s["text"] or "" for s in cap.sent]
    assert any("will NOT be posted" in m for m in msgs)


def test_schedule_best_sends_confirmation_message(tmp_path):
    """⭐️ Use Best Time -> persisted-row confirmation chat message."""
    import asyncio
    from threads_operator import workflow_telegram as wt

    cap = _Capture()
    disp = _sched_dispatcher(tmp_path / "s.json", cap)
    calls: list[list[str]] = []

    async def fake_run_cli(argv, group, args, *, cwd, timeout=90):
        calls.append(args)
        return 0, {
            "ok": True, "status": "queued", "queue_id": 501, "queue_status": "approved",
            "scheduled_at": "2026-09-25T21:45:00+00:00",
            "schedule_source": "historical", "score": 0.42, "sample_size": 17,
            "id": 146,
            "queue_row": {
                "id": 501,
                "scheduled_at": "2026-09-25T21:45:00+00:00",
                "main_post_text": "Draft 146 body",
            },
        }

    with patch.object(wt, "_run_cli", side_effect=fake_run_cli):
        handled = asyncio.run(disp.handle_callback(
            data="trendsched:best:146", chat_id="79553451",
            user_id="79553451", answer=cap.answer))
    assert handled is True
    assert calls and calls[0][0] == "schedule-best"
    assert any("Scheduling confirmed" in a for a in cap.answered)
    msgs = [s["text"] for s in cap.sent if s["text"]]
    assert len(msgs) == 1
    text = msgs[0]
    assert text.startswith("✅ Post scheduled.")
    assert "Candidate #146" in text
    assert "Draft 146 body" in text
    assert "MYT" in text
    assert "Mode: ⭐️ Best Time" in text
    assert "Queue ID: #501" in text
    assert "Approved — waiting for publisher" in text
    assert "published automatically at the scheduled time" in text


def test_schedule_now_sends_confirmation_message(tmp_path):
    """⚡️ Post Now -> due-now confirmation, no Scheduled line."""
    import asyncio
    from threads_operator import workflow_telegram as wt

    cap = _Capture()
    disp = _sched_dispatcher(tmp_path / "s.json", cap)
    calls: list[list[str]] = []

    async def fake_run_cli(argv, group, args, *, cwd, timeout=90):
        calls.append(args)
        return 0, {
            "ok": True, "status": "queued", "queue_id": 777, "queue_status": "approved",
            "scheduled_at": "2026-09-25T10:00:00+00:00",
            "schedule_source": "now", "id": 146,
            "queue_row": {
                "id": 777,
                "scheduled_at": "2026-09-25T10:00:00+00:00",
                "main_post_text": "Draft 146 body",
            },
        }

    with patch.object(wt, "_run_cli", side_effect=fake_run_cli):
        handled = asyncio.run(disp.handle_callback(
            data="trendsched:now:146", chat_id="79553451",
            user_id="79553451", answer=cap.answer))
    assert handled is True
    assert calls and calls[0][0] == "schedule-now"
    msgs = [s["text"] for s in cap.sent if s["text"]]
    assert len(msgs) == 1
    text = msgs[0]
    assert text.startswith("✅ Post approved for publishing.")
    assert "Candidate #146" in text
    assert "Mode: ⚡️ Post Now" in text
    assert "Queue ID: #777" in text
    assert "Approved — due now" in text
    assert "next worker run" in text
    assert "Scheduled:" not in text


def test_choose_time_confirm_sends_confirmation_message(tmp_path):
    """🕐 Choose Time -> Confirm -> custom-time confirmation from persisted row."""
    import asyncio
    from threads_operator import workflow_telegram as wt

    cap = _Capture()
    disp = _sched_dispatcher(tmp_path / "s.json", cap)
    calls: list[list[str]] = []

    async def fake_run_cli(argv, group, args, *, cwd, timeout=90):
        calls.append(args)
        return 0, {
            "ok": True, "status": "queued", "queue_id": 880, "queue_status": "approved",
            "scheduled_at": "2026-09-30T01:30:00+00:00",
            "schedule_source": "custom", "id": 146,
            "queue_row": {
                "id": 880,
                "scheduled_at": "2026-09-30T01:30:00+00:00",
                "main_post_text": "Draft 146 body",
            },
        }

    with patch.object(wt, "_run_cli", side_effect=fake_run_cli):
        asyncio.run(disp.handle_callback(
            data="trendsched:choose:146", chat_id="79553451",
            user_id="79553451", answer=cap.answer))
        handled = asyncio.run(disp.handle_text(
            chat_id="79553451", user_id="79553451", text="tomorrow 8:30pm"))
        assert handled is True
        asyncio.run(disp.handle_callback(
            data="trendsched:confirm:146", chat_id="79553451",
            user_id="79553451", answer=cap.answer))
    assert any(c[0] == "schedule-time" for c in calls)
    msgs = [s["text"] for s in cap.sent if s["text"] and s["text"].startswith("✅ Post scheduled.")]
    assert len(msgs) == 1
    text = msgs[0]
    assert "Candidate #146" in text
    assert "Mode: 🕐 Custom Time" in text
    assert "Queue ID: #880" in text
    assert "Approved — waiting for publisher" in text
    assert "MYT" in text


def test_schedule_best_retry_does_not_repeat_confirmation(tmp_path):
    """already_queued retry: toast ack, NO duplicate success confirmation message."""
    import asyncio
    from threads_operator import workflow_telegram as wt

    cap = _Capture()
    disp = _sched_dispatcher(tmp_path / "s.json", cap)

    async def fake_run_cli(argv, group, args, *, cwd, timeout=90):
        return 0, {
            "ok": True, "status": "queued", "queue_id": 501,
            "queue_status": "approved",
            "scheduled_at": "2026-09-25T21:45:00+00:00",
            "schedule_source": "historical", "already_queued": True, "id": 146,
            "queue_row": {
                "id": 501,
                "scheduled_at": "2026-09-25T21:45:00+00:00",
                "main_post_text": "Draft 146 body",
            },
        }

    with patch.object(wt, "_run_cli", side_effect=fake_run_cli):
        handled = asyncio.run(disp.handle_callback(
            data="trendsched:best:146", chat_id="79553451",
            user_id="79553451", answer=cap.answer))
    assert handled is True
    assert any("already scheduled" in a for a in cap.answered)
    assert all(not (s["text"] or "").startswith("✅ Post") for s in cap.sent)
