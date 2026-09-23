"""Deterministic tests for rebuild_rollups.py pure logic + CLI guards.

No network: builds fake snapshot rows and asserts aggregation math, MYT
boundaries, anomaly reporting, and the completed-day-only gate.
"""

import importlib.util
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "rebuild_rollups", ROOT / "scripts" / "rebuild_rollups.py"
)
rr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rr)


def snap(ts_iso, views=None, likes=None, replies=None, reposts=None,
          quotes=None, shares=None, clicks=None, followers=None,
          published_at=None, age=None, post_id="P1", account_id="A", pk=None):
    ts = datetime.fromisoformat(ts_iso)
    if pk is None:
        pk = ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    row = {
        "captured_at": pk,
        "account_id": account_id,
        "post_id": post_id,
        "published_at": published_at,
        "post_age_minutes": age,
        "views": views, "likes": likes, "replies": replies,
        "reposts": reposts, "quotes": quotes, "shares": shares,
        "clicks": clicks, "followers_count": followers,
    }
    row["_ts"] = ts
    return row


# ------------------------------------------------------------- MYT boundaries


def test_myt_day_bounds_use_utc_plus_8():
    start, end = rr.myt_day_bounds(date(2026, 9, 11))
    assert start == datetime(2026, 9, 10, 16, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 11, 16, 0, tzinfo=timezone.utc)


def test_myt_date_of_handles_utc_crossing():
    # 2026-09-10 16:30 UTC = 2026-09-11 00:30 MYT
    ts = datetime(2026, 9, 10, 16, 30, tzinfo=timezone.utc)
    assert rr.myt_date_of(ts) == date(2026, 9, 11)


# -------------------------------------------------------------- post rollup


def test_post_rollup_basic_gains_and_age():
    day = date(2026, 9, 11)
    rows = [
        snap("2026-09-11T08:00:00+08:00", views=100, likes=10, replies=2,
             reposts=1, quotes=0, shares=3, age=60,
             published_at="2026-09-11T07:00:00+08:00"),
        snap("2026-09-11T09:00:00+08:00", views=160, likes=13, replies=3,
             reposts=1, quotes=1, shares=3, age=120),
        snap("2026-09-11T10:00:00+08:00", views=220, likes=20, replies=5,
             reposts=2, quotes=1, shares=4, age=180),
    ]
    r = rr.build_post_rollup(day, "A", "P1", rows)
    assert r["snapshot_count"] == 3
    assert r["views_start"] == 100 and r["views_end"] == 220 and r["views_gained"] == 120
    assert r["likes_gained"] == 10
    assert r["replies_gained"] == 3
    assert r["reposts_gained"] == 1
    assert r["quotes_gained"] == 1
    assert r["shares_gained"] == 1
    # engagement = likes + replies + reposts + quotes + shares deltas
    assert r["engagement_gained"] == 10 + 3 + 1 + 1 + 1
    assert r["age_minutes_start"] == 60 and r["age_minutes_end"] == 180
    assert r["published_at"] == "2026-09-11T07:00:00+08:00"
    # 60 views/h then 60 views/h -> max 60.0; ties keep the earliest interval (deterministic)
    assert r["max_view_velocity_per_hour"] == 60.0
    assert r["peak_velocity_at"].startswith("2026-09-11T09:00:00")
    assert r["anomalies"] is None
    assert r["first_captured_at"].startswith("2026-09-11T08:00:00")
    assert r["last_captured_at"].startswith("2026-09-11T10:00:00")


def test_post_rollup_uses_prior_day_snapshot_as_open():
    day = date(2026, 9, 11)
    rows = [
        snap("2026-09-10T23:00:00+08:00", views=500, likes=50, replies=5,
             reposts=5, quotes=0, shares=0),
        snap("2026-09-11T00:30:00+08:00", views=530, likes=52, replies=5,
             reposts=5, quotes=1, shares=0),
    ]
    r = rr.build_post_rollup(day, "A", "P1", rows)
    assert r["snapshot_count"] == 1  # only the in-day snapshot counts
    assert r["views_start"] == 500 and r["views_gained"] == 30
    assert r["likes_gained"] == 2
    assert r["quotes_gained"] == 1
    assert r["replies_gained"] == 0
    assert r["max_view_velocity_per_hour"] is None  # needs >=2 in-day snapshots
    assert r["peak_velocity_at"] is None


def test_post_rollup_negative_delta_reported_not_hidden():
    day = date(2026, 9, 11)
    rows = [
        snap("2026-09-11T08:00:00+08:00", views=300, likes=30, replies=1,
             reposts=0, quotes=0, shares=0),
        snap("2026-09-11T09:00:00+08:00", views=12, likes=1, replies=0,
             reposts=0, quotes=0, shares=0),
    ]
    notes: list[str] = []
    r = rr.build_post_rollup(day, "A", "P1", rows, anomalies_out=notes)
    assert r["views_gained"] == -288  # kept raw; anomaly explains it
    assert "views_reset(300->12)" in r["anomalies"]
    assert "likes_reset(30->1)" in r["anomalies"]
    assert any("views_reset" in n and "P1" in n for n in notes)


def test_post_rollup_skips_all_null_snapshots():
    day = date(2026, 9, 11)
    rows = [
        snap("2026-09-11T08:00:00+08:00"),  # every metric None
        snap("2026-09-11T09:00:00+08:00", views=10, likes=1, replies=0,
             reposts=0, quotes=0, shares=0),
    ]
    r = rr.build_post_rollup(day, "A", "P1", rows)
    assert r["snapshot_count"] == 1
    assert "all_null_snapshots_dropped=1" in r["anomalies"]


def test_post_rollup_returns_none_when_nothing_usable():
    day = date(2026, 9, 11)
    assert rr.build_post_rollup(day, "A", "P1", [snap("2026-09-11T08:00:00+08:00")]) is None


def test_post_rollup_single_snapshot_delta_null():
    day = date(2026, 9, 11)
    rows = [snap("2026-09-11T08:00:00+08:00", views=40, likes=4, replies=0,
                 reposts=0, quotes=0, shares=0)]
    r = rr.build_post_rollup(day, "A", "P1", rows)
    assert r["views_start"] == 40 and r["views_end"] == 40
    assert r["views_gained"] is None  # zero elapsed span -> gain unknown, not 0
    assert r["engagement_gained"] is None


# ------------------------------------------------------------ account rollup


def test_account_rollup_fields_and_growth():
    day = date(2026, 9, 11)
    rows = [
        snap("2026-09-11T07:00:00+08:00", views=1000, likes=100, replies=10,
             reposts=5, quotes=2, clicks=50, followers=1000),
        snap("2026-09-11T20:00:00+08:00", views=1500, likes=140, replies=15,
             reposts=6, quotes=2, clicks=80, followers=1050),
    ]
    r = rr.build_account_rollup(day, "A", rows, posts_published=3)
    assert r["followers_start"] == 1000 and r["followers_end"] == 1050
    assert r["followers_gained"] == 50
    assert r["follower_growth_pct"] == 5.0
    assert r["views_gained"] == 500
    assert r["likes_gained"] == 40
    assert r["replies_gained"] == 5
    assert r["reposts_gained"] == 1
    assert r["quotes_gained"] == 0
    assert r["profile_views_gained"] is None  # no trustworthy source
    assert r["posts_published"] == 3
    assert "clicks" not in json.dumps(r)  # not smuggled into the payload


def test_account_rollup_reset_anomaly():
    day = date(2026, 9, 11)
    rows = [
        snap("2026-09-11T07:00:00+08:00", followers=1050, views=10, likes=1,
             replies=0, reposts=0, quotes=0, clicks=0),
        snap("2026-09-11T20:00:00+08:00", followers=900, views=10, likes=1,
             replies=0, reposts=0, quotes=0, clicks=0),
    ]
    notes: list[str] = []
    r = rr.build_account_rollup(day, "A", rows, 0, notes)
    assert r["followers_gained"] == -150
    assert any("followers_count_reset(1050->900)" in n for n in notes)


def test_account_rollup_none_when_no_usable_rows():
    day = date(2026, 9, 11)
    rows = [
        {**snap("2026-09-11T07:00:00+08:00"), "views": None, "likes": None,
         "replies": None, "reposts": None, "quotes": None, "clicks": None,
         "followers_count": None},
    ]
    assert rr.build_account_rollup(day, "A", rows, 0) is None


# ---------------------------------------------------------------- CLI guards


def test_cli_refuses_today_without_flag():
    # run at a fixed "now" mid-2026-09-12 MYT; asking for 09-12 must fail rc=2
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "rebuild_rollups.py"),
         "--date", "2026-09-12", "--now", "2026-09-12T10:00:00+08:00", "--dry-run"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "incomplete MYT day" in proc.stderr


def test_cli_requires_date_args():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "rebuild_rollups.py")],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0


def test_daterange_inclusive():
    days = list(rr.daterange(date(2026, 9, 11), date(2026, 9, 13)))
    assert days == [date(2026, 9, 11), date(2026, 9, 12), date(2026, 9, 13)]
