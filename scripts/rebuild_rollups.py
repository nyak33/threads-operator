#!/usr/bin/env python3
"""Rebuild daily rollups for the Threads Insights Collector.

Deterministic, idempotent, safe to re-run:
  - Source of truth: threads_account_insights_snapshots / threads_post_insights_snapshots
    (read-only; never mutated).
  - Destinations: threads_daily_rollups (account-level only) and
    threads_post_daily_rollups (post-level), both rebuilt per (date, account) via
    DELETE-then-INSERT so reruns can never duplicate or leave stale columns.
  - Dates are interpreted in Asia/Kuala_Lumpur (MYT, UTC+8, no DST).
  - Completed MYT days only unless --allow-provisional is passed.
  - Missing metrics stay NULL (unknown), never coerced to 0.
  - Counter resets / negative deltas are reported in the anomalies column
    (post table) and on stderr (both tables) instead of being guessed away.

Usage:
  rebuild_rollups.py --date 2026-09-11
  rebuild_rollups.py --from 2026-09-11 --to 2026-09-12 [--allow-provisional] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

MYT = timezone(timedelta(hours=8), "MYT")
MYT_DATE_PREFIX_LEN = 10  # len("YYYY-MM-DD")

ACCOUNT_SNAP_TABLE = "threads_account_insights_snapshots"
POST_SNAP_TABLE = "threads_post_insights_snapshots"
ACCOUNT_ROLLUP_TABLE = "threads_daily_rollups"
POST_ROLLUP_TABLE = "threads_post_daily_rollups"

POST_METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")
ENGAGEMENT_METRICS = ("likes", "replies", "reposts", "quotes", "shares")
ACCOUNT_METRICS = (
    "views",
    "likes",
    "replies",
    "reposts",
    "quotes",
    "clicks",
    "followers_count",
)

LOOKBACK = timedelta(hours=37)  # covers an overnight collection pause before day start
PAGE_SIZE = 1000


class RollupError(RuntimeError):
    pass


# --------------------------------------------------------------------------- io


class RestClient:
    """Minimal Supabase PostgREST client (stdlib only; same env contract as collector)."""

    def __init__(self, base_url: str, key: str) -> None:
        if not base_url or not key:
            raise RollupError("SUPABASE_URL and a Supabase key are required")
        self.base_url = base_url.rstrip("/")
        self.key = key

    def _headers(self, prefer: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def select(self, table: str, params: list[tuple[str, str]]) -> list[dict[str, Any]]:
        url = f"{self.base_url}/rest/v1/{table}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise RollupError(f"SELECT {table} HTTP {exc.code}: {exc.read().decode()[:200]}") from exc

    def delete(self, table: str, params: dict[str, str]) -> None:
        url = f"{self.base_url}/rest/v1/{table}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=self._headers(), method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            raise RollupError(f"DELETE {table} HTTP {exc.code}: {exc.read().decode()[:200]}") from exc

    def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        url = f"{self.base_url}/rest/v1/{table}"
        req = urllib.request.Request(
            url, data=json.dumps(rows).encode(), headers=self._headers("return=minimal")
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            raise RollupError(f"INSERT {table} HTTP {exc.code}: {exc.read().decode()[:200]}") from exc


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


# ------------------------------------------------------------------- time utils


def myt_day_bounds(day: date) -> tuple[datetime, datetime]:
    """[start, end) of a MYT calendar day, as aware UTC datetimes."""
    start = datetime(day.year, day.month, day.day, tzinfo=MYT)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def myt_date_of(ts: datetime) -> date:
    return ts.astimezone(MYT).date()


def parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).replace("Z", "+00:00")
    ts = datetime.fromisoformat(text)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def daterange(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


# ------------------------------------------------------------------- pure logic


def usable(row: dict[str, Any], metrics: tuple[str, ...]) -> bool:
    """A snapshot is usable for aggregation when at least one core metric is present.

    Matches the collector's has_usable_metrics rule: NULL means unknown, not zero.
    """
    return any(row.get(name) is not None for name in metrics)


def _sorted_by_capture(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # id as tie-breaker makes ordering deterministic when captured_at collides
    return sorted(rows, key=lambda r: (r["_ts"], r.get("id") or ""))


def _metric_span(day_rows: list[dict[str, Any]], prior_row: dict[str, Any] | None, name: str):
    """(start, end) value for a metric using the last snapshot before day start as open."""
    opening = None
    if prior_row is not None and prior_row.get(name) is not None:
        opening = prior_row[name]
    closing = None
    for row in day_rows:  # day_rows sorted ascending by captured_at
        if row.get(name) is not None:
            if opening is None:
                opening = row[name]  # no pre-day snapshot: first in-day value is the floor
            closing = row[name]
    return opening, closing


def build_post_rollup(
    day: date,
    account_id: str,
    post_id: str,
    rows: list[dict[str, Any]],
    anomalies_out: list[str] | None = None,
) -> dict[str, Any] | None:
    """Aggregate one post's snapshots for one MYT day. Returns None if nothing usable."""
    day_rows = _sorted_by_capture(
        [r for r in rows if usable(r, POST_METRICS) and myt_date_of(r["_ts"]) == day]
    )
    if not day_rows:
        return None
    dropped = len([r for r in rows if myt_date_of(r["_ts"]) == day]) - len(day_rows)

    start_utc, end_utc = myt_day_bounds(day)
    prior = [r for r in _sorted_by_capture(rows) if r["_ts"] < start_utc]
    prior_row = prior[-1] if prior else None

    rollup: dict[str, Any] = {
        "date": day.isoformat(),
        "account_id": account_id,
        "post_id": post_id,
        "published_at": next(
            (r.get("published_at") for r in day_rows if r.get("published_at")), None
        ),
        "snapshot_count": len(day_rows),
        "first_captured_at": day_rows[0]["_ts"].isoformat(),
        "last_captured_at": day_rows[-1]["_ts"].isoformat(),
        "age_minutes_start": day_rows[0].get("post_age_minutes"),
        "age_minutes_end": day_rows[-1].get("post_age_minutes"),
        "rebuilt_at": None,  # filled by caller
    }

    anomalies: list[str] = []
    if dropped:
        anomalies.append(f"all_null_snapshots_dropped={dropped}")

    # a lone in-day snapshot with no pre-day snapshot gives start==end from the
    # same observation: value known, delta NOT (stay NULL, do not report 0)
    single_observation = prior_row is None and len(day_rows) == 1

    for metric in ("views", "likes"):
        opening, closing = _metric_span(day_rows, prior_row, metric)
        rollup[f"{metric}_start"] = opening
        rollup[f"{metric}_end"] = closing
        gain = None
        if opening is not None and closing is not None and not single_observation:
            gain = closing - opening
            if gain < 0:
                anomalies.append(f"{metric}_reset({opening}->{closing})")
        rollup[f"{metric}_gained"] = gain

    engagement_total = None
    for metric in ("replies", "reposts", "quotes", "shares"):
        opening, closing = _metric_span(day_rows, prior_row, metric)
        gain = None
        if opening is not None and closing is not None and not single_observation:
            gain = closing - opening
            if gain < 0:
                anomalies.append(f"{metric}_reset({opening}->{closing})")
        rollup[f"{metric}_gained"] = gain
        if gain is not None:
            engagement_total = (engagement_total or 0) + gain
    # likes participates in engagement too
    if rollup["likes_gained"] is not None:
        engagement_total = (engagement_total or 0) + rollup["likes_gained"]
    if engagement_total is not None and engagement_total < 0:
        anomalies.append(f"engagement_reset({engagement_total})")
    rollup["engagement_gained"] = engagement_total

    # max view velocity between consecutive in-day usable snapshots
    max_velocity = None
    peak_at = None
    for prev, cur in zip(day_rows, day_rows[1:]):
        if prev.get("views") is None or cur.get("views") is None:
            continue
        elapsed_min = (cur["_ts"] - prev["_ts"]).total_seconds() / 60.0
        if elapsed_min <= 0:
            continue
        velocity = (cur["views"] - prev["views"]) / elapsed_min * 60.0
        if max_velocity is None or velocity > max_velocity:
            max_velocity = velocity
            peak_at = cur["_ts"]
    rollup["max_view_velocity_per_hour"] = (
        round(max_velocity, 4) if max_velocity is not None else None
    )
    rollup["peak_velocity_at"] = peak_at.isoformat() if peak_at else None

    if anomalies:
        rollup["anomalies"] = ";".join(anomalies)
        if anomalies_out is not None:
            for note in anomalies:
                anomalies_out.append(f"{day.isoformat()} post {post_id}: {note}")
    else:
        rollup["anomalies"] = None
    return rollup


def build_account_rollup(
    day: date,
    account_id: str,
    rows: list[dict[str, Any]],
    posts_published: int,
    anomalies_out: list[str] | None = None,
) -> dict[str, Any] | None:
    """Aggregate account snapshots for one MYT day into the legacy-shaped account row."""
    day_rows = _sorted_by_capture(
        [r for r in rows if usable(r, ACCOUNT_METRICS) and myt_date_of(r["_ts"]) == day]
    )
    if not day_rows:
        return None

    start_utc, _end_utc = myt_day_bounds(day)
    prior = [r for r in _sorted_by_capture(rows) if r["_ts"] < start_utc]
    prior_row = prior[-1] if prior else None

    rollup: dict[str, Any] = {
        "date": day.isoformat(),
        "account_id": account_id,
        "posts_published": posts_published,
        "rebuilt_at": None,
    }
    anomalies: list[str] = []

    # same rule as post rollups: a lone observation yields a value, not a delta
    single_observation = prior_row is None and len(day_rows) == 1

    def gained(name: str, col: str) -> tuple[int | None, int | None]:
        opening, closing = _metric_span(day_rows, prior_row, name)
        delta = None
        if opening is not None and closing is not None and not single_observation:
            delta = closing - opening
            if delta < 0:
                anomalies.append(f"{name}_reset({opening}->{closing})")
        rollup[col] = delta
        return opening, closing

    followers_open, followers_close = gained("followers_count", "followers_gained")
    rollup["followers_start"] = followers_open
    rollup["followers_end"] = followers_close
    if rollup["followers_gained"] is not None and followers_open:
        rollup["follower_growth_pct"] = round(
            rollup["followers_gained"] / followers_open * 100.0, 4
        )
    else:
        rollup["follower_growth_pct"] = None

    # live table has no clicks column — deltas still reported via anomalies/stderr
    for name, col in (
        ("views", "views_gained"),
        ("likes", "likes_gained"),
        ("replies", "replies_gained"),
        ("reposts", "reposts_gained"),
        ("quotes", "quotes_gained"),
    ):
        gained(name, col)
    clicks_open, clicks_close = gained("clicks", "_clicks_delta")  # not persisted
    rollup["profile_views_gained"] = None  # no trustworthy source field; stays NULL

    rollup.pop("_clicks_delta", None)
    if anomalies:
        if anomalies_out is not None:
            for note in anomalies:
                anomalies_out.append(f"{day.isoformat()} account {account_id}: {note}")
    return rollup


# ------------------------------------------------------------------- data flow


def fetch_snapshots(client: RestClient, table: str, window: tuple[datetime, datetime]) -> list[dict[str, Any]]:
    """Window is [since, until) as aware datetimes; repeated captured_at
    filters are encoded as tuples (a dict would collapse duplicate keys)."""
    since, until = window
    base_params = [
        ("captured_at", f"gte.{since.isoformat()}"),
        ("captured_at", f"lt.{until.isoformat()}"),
        ("order", "captured_at.asc,id.asc"),
        ("limit", str(PAGE_SIZE)),
        ("select", "*"),
    ]
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.select(table, [*base_params, ("offset", str(offset))])
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    for row in rows:
        row["_ts"] = parse_ts(row["captured_at"])
    return rows


def group_by_key(rows: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row[key], []).append(row)
    return grouped


def compute_for_range(
    client: RestClient,
    days: list[date],
    anomalies_out: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Fetch the raw window covering `days` and compute both rollup sets."""
    first_start, _ = myt_day_bounds(days[0])
    _, last_end = myt_day_bounds(days[-1])
    window = (first_start - LOOKBACK, last_end)

    account_rows = fetch_snapshots(client, ACCOUNT_SNAP_TABLE, window)
    post_rows = fetch_snapshots(client, POST_SNAP_TABLE, window)

    target_days = {d.isoformat() for d in days}
    account_rollups: list[dict[str, Any]] = []
    post_rollups: list[dict[str, Any]] = []

    # (account, day) -> posts published on that MYT day, for posts_published
    published_counts: dict[tuple[str, str], int] = {}
    posts_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in post_rows:
        posts_by_key.setdefault((row["account_id"], row["post_id"]), []).append(row)

    for (acct, post_id), rows in sorted(posts_by_key.items()):
        per_day: dict[date, list[dict[str, Any]]] = {}
        for row in rows:
            per_day.setdefault(myt_date_of(row["_ts"]), []).append(row)
        for day in days:
            day_rows = per_day.get(day)
            if not day_rows:
                continue
            pub_ts = next((r.get("published_at") for r in day_rows if r.get("published_at")), None)
            if pub_ts and myt_date_of(parse_ts(pub_ts)) == day:
                published_counts[(acct, day.isoformat())] = (
                    published_counts.get((acct, day.isoformat()), 0) + 1
                )
            rollup = build_post_rollup(day, acct, post_id, day_rows, anomalies_out)
            if rollup:
                post_rollups.append(rollup)

    accounts_by_acct = group_by_key(account_rows, "account_id")
    for acct, rows in sorted(accounts_by_acct.items()):
        per_day: dict[date, list[dict[str, Any]]] = {}
        for row in rows:
            per_day.setdefault(myt_date_of(row["_ts"]), []).append(row)
        for day in days:
            all_rows = per_day.get(day)
            if not all_rows:
                continue
            rollup = build_account_rollup(
                day,
                acct,
                [r for r in all_rows],
                published_counts.get((acct, day.isoformat()), 0),
                anomalies_out,
            )
            if rollup:
                account_rollups.append(rollup)

    return {"account": account_rollups, "post": post_rollups}


def write_rollups(client: RestClient, days: list[date], data: dict[str, list[dict[str, Any]]]) -> None:
    """Idempotent replace: delete target-day rows, then insert freshly computed ones."""
    day_list = ",".join(d.isoformat() for d in days)
    rebuilt_at = datetime.now(timezone.utc).isoformat()
    for table, rows in (
        (ACCOUNT_ROLLUP_TABLE, data["account"]),
        (POST_ROLLUP_TABLE, data["post"]),
    ):
        for row in rows:
            row["rebuilt_at"] = rebuilt_at
        client.delete(table, {"date": f"in.({day_list})"})
        for i in range(0, len(rows), 200):
            client.insert(table, rows[i : i + 200])


# ------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", type=date.fromisoformat, help="MYT calendar date YYYY-MM-DD")
    parser.add_argument("--from", dest="from_date", type=date.fromisoformat)
    parser.add_argument("--to", dest="to_date", type=date.fromisoformat)
    parser.add_argument(
        "--allow-provisional",
        action="store_true",
        help="permit rebuilding a MYT day that has not fully closed yet",
    )
    parser.add_argument("--dry-run", action="store_true", help="compute and print, write nothing")
    parser.add_argument("--now", default=None, help=argparse.SUPPRESS)  # test hook
    args = parser.parse_args(argv)

    if args.date and (args.from_date or args.to_date):
        parser.error("--date is mutually exclusive with --from/--to")
    if args.date:
        days = [args.date]
    elif args.from_date and args.to_date:
        if args.from_date > args.to_date:
            parser.error("--from must be <= --to")
        days = list(daterange(args.from_date, args.to_date))
    else:
        parser.error("provide --date or --from/--to")

    now = parse_ts(args.now) if args.now else datetime.now(timezone.utc)
    if not args.allow_provisional:
        today_myt = myt_date_of(now)
        incomplete = [d for d in days if d >= today_myt]
        if incomplete:
            print(
                f"refusing incomplete MYT day(s): {[d.isoformat() for d in incomplete]} "
                f"(today is {today_myt.isoformat()} MYT); pass --allow-provisional to override",
                file=sys.stderr,
            )
            return 2

    load_env(Path(__file__).resolve().parents[1] / ".env")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SECRET_KEY")
    client = RestClient(os.environ.get("SUPABASE_URL", ""), key or "")

    anomalies: list[str] = []
    data = compute_for_range(client, days, anomalies)
    account_rows, post_rows_out = data["account"], data["post"]

    if args.dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "days": [d.isoformat() for d in days],
                    "account_rollups": len(account_rows),
                    "post_rollups": len(post_rows_out),
                    "anomaly_count": len(anomalies),
                },
                indent=2,
            )
        )
    else:
        write_rollups(client, days, data)
        print(
            json.dumps(
                {
                    "mode": "write",
                    "days": [d.isoformat() for d in days],
                    "account_rollups": len(account_rows),
                    "post_rollups": len(post_rows_out),
                    "anomaly_count": len(anomalies),
                },
                indent=2,
            )
        )

    if anomalies:
        print(f"anomalies ({len(anomalies)}):", file=sys.stderr)
        for note in anomalies:
            print(f"  {note}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
