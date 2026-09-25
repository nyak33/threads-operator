"""Deterministic smart scheduling for approved trend-engagement content.

This module answers one question: *when* should an already-approved post be
published? It deliberately keeps the first version practical (no ML) — a
deterministic scoring function over the account's own historical posting
performance, with a graceful fallback to configured posting windows when the
history is too thin to be trusted.

Data source
-----------
``threads_post_insights_snapshots`` (Supabase), one row per capture of one of
the account's own posts. Each row carries ``published_at`` plus engagement
metrics (``views``, ``likes``, ``replies``, ``reposts``, ``quotes``). The
latest capture per post (largest ``captured_at`` / ``post_age_minutes``) is the
post's final observed performance.

Scoring
-------
For each post we compute a *normalized engagement rate* so a high-view /
low-interaction post does not dominate a lower-view but engaging one::

    engagement = likes + replies + reposts + quotes
    rate       = engagement / views            (views > 0)
                 0.0                            (views missing / 0 — ignored)

Every post's ``published_at`` is converted to the user-facing timezone
(Asia/Kuala_Lumpur) and bucketed by (day_of_week, hour). A bucket is only
trusted when it has at least ``min_samples`` posts; trusted buckets are ranked
by mean engagement-rate (ties broken by larger sample, then later hour).

Slot selection
--------------
Candidate publish slots are generated on an hourly grid starting from
``min_lead_minutes`` in the future, looking ahead ``lookahead_days``. The
first slot whose (dow, hour) bucket is trusted *and* which does not collide
with an existing queue row within ``min_gap_minutes`` is selected. When no
trusted bucket yields a collision-free slot, the scheduler degrades to the
configured fallback windows (``fallback_hours_local``) — it never fails the
approval and never leaves content in draft.

Custom time parsing is MYT-first and returns an aware UTC timestamp so the
queue row stores timestamptz in the database's existing UTC convention.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

MYT = ZoneInfo("Asia/Kuala_Lumpur")

ENGAGEMENT_METRICS = ("likes", "replies", "reposts", "quotes")


@dataclass(frozen=True)
class SchedulingConfig:
    """Tunable knobs for smart scheduling. Single source of truth (no magic
    numbers scattered through call sites)."""

    min_gap_minutes: int = 60          # min spacing vs existing queue rows
    min_samples: int = 2               # posts required to trust a bucket
    lookahead_days: int = 7            # how far ahead to search for a slot
    min_lead_minutes: int = 5          # never schedule in the immediate past
    fallback_hours_local: tuple[int, ...] = (9, 13, 20)  # MYT fallback windows
    history_window_days: int = 90      # only score posts published within this
    max_insight_rows: int = 5000       # cap on rows pulled for scoring


@dataclass(frozen=True)
class SlotRecommendation:
    scheduled_utc: datetime            # aware UTC timestamp for the queue row
    source: str                        # 'historical' | 'fallback'
    day_of_week: int                   # MYT weekday (Mon=0)
    hour_local: int                    # MYT hour
    score: float                       # engagement-rate score (0.0 for fallback)
    sample_size: int                   # posts backing this bucket (0 for fallback)
    reason: str                        # human/log explanation


# ---------------------------------------------------------------------------
# Engagement-rate scoring
# ---------------------------------------------------------------------------

def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _engagement_rate(row: dict[str, Any]) -> float | None:
    """Normalized engagement rate for one post snapshot, or None if unusable."""
    try:
        views = float(row.get("views") or 0)
    except (TypeError, ValueError):
        return None
    if views <= 0:
        return None
    engagement = 0.0
    for name in ENGAGEMENT_METRICS:
        try:
            engagement += float(row.get(name) or 0)
        except (TypeError, ValueError):
            continue
    return engagement / views


def _latest_per_post(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Collapse many captures per post into the single latest capture."""
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        post_id = row.get("post_id")
        if not post_id:
            continue
        captured = _parse_ts(row.get("captured_at"))
        age = row.get("post_age_minutes")
        key = (captured or datetime.min.replace(tzinfo=timezone.utc),
               float(age) if isinstance(age, (int, float)) else 0.0)
        prev = latest.get(post_id)
        if prev is None:
            latest[post_id] = {"row": row, "_k": key}
        else:
            if key > prev["_k"]:
                latest[post_id] = {"row": row, "_k": key}
    return {pid: v["row"] for pid, v in latest.items()}


def score_time_buckets(
    insight_rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    config: SchedulingConfig | None = None,
) -> dict[tuple[int, int], dict[str, Any]]:
    """Score (day_of_week, hour_local) buckets by mean engagement rate.

    Returns a mapping of bucket -> {score, samples}. Only buckets meeting the
    minimum sample size are included.
    """
    cfg = config or SchedulingConfig()
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=cfg.history_window_days)

    latest = _latest_per_post(insight_rows)
    buckets: dict[tuple[int, int], list[float]] = {}
    for row in latest.values():
        published = _parse_ts(row.get("published_at"))
        if published is None or published < cutoff:
            continue
        rate = _engagement_rate(row)
        if rate is None:
            continue
        local = published.astimezone(MYT)
        buckets.setdefault((local.weekday(), local.hour), []).append(rate)

    scored: dict[tuple[int, int], dict[str, Any]] = {}
    for bucket, rates in buckets.items():
        if len(rates) < cfg.min_samples:
            continue
        scored[bucket] = {"score": sum(rates) / len(rates), "samples": len(rates)}
    return scored


# ---------------------------------------------------------------------------
# Slot selection with collision protection
# ---------------------------------------------------------------------------

def _is_collision(
    candidate: datetime,
    occupied: list[datetime],
    *,
    min_gap: timedelta,
) -> bool:
    for taken in occupied:
        if abs(candidate - taken) < min_gap:
            return True
    return False


def recommend_slot(
    insight_rows: list[dict[str, Any]],
    occupied_utc: list[datetime],
    *,
    now: datetime | None = None,
    config: SchedulingConfig | None = None,
) -> SlotRecommendation:
    """Pick the best upcoming publish slot.

    ``occupied_utc`` are the scheduled_at timestamps of already approved /
    scheduled queue rows (collision avoidance). Falls back to configured
    windows when history is insufficient or every trusted slot is occupied.
    """
    cfg = config or SchedulingConfig()
    now = now or datetime.now(timezone.utc)
    min_gap = timedelta(minutes=cfg.min_gap_minutes)
    scored = score_time_buckets(insight_rows, now=now, config=cfg)

    # Rank trusted buckets: score desc, samples desc, hour desc.
    ranked = sorted(
        scored.items(),
        key=lambda kv: (kv[1]["score"], kv[1]["samples"], kv[0][1]),
        reverse=True,
    )

    earliest = now + timedelta(minutes=cfg.min_lead_minutes)
    horizon = now + timedelta(days=cfg.lookahead_days)

    # Try trusted buckets in rank order; find the next upcoming occurrence.
    for (dow, hour), stat in ranked:
        slot = _next_occurrence(earliest, dow, hour)
        while slot is not None and slot <= horizon:
            if not _is_collision(slot, occupied_utc, min_gap=min_gap):
                logger.info(
                    "smart_schedule: historical slot dow=%d hour=%d score=%.4f "
                    "samples=%d -> %s",
                    dow, hour, stat["score"], stat["samples"], slot.isoformat(),
                )
                return SlotRecommendation(
                    scheduled_utc=slot,
                    source="historical",
                    day_of_week=dow,
                    hour_local=hour,
                    score=stat["score"],
                    sample_size=stat["samples"],
                    reason=f"best historical engagement slot (score={stat['score']:.4f}, n={stat['samples']})",
                )
            slot = _next_occurrence(slot + timedelta(hours=1), dow, hour)

    # Fallback: configured windows, in order, first non-colliding future slot.
    fallback = _fallback_slot(earliest, horizon, occupied_utc, min_gap, cfg)
    if fallback is not None:
        local = fallback.astimezone(MYT)
        logger.info(
            "smart_schedule: fallback slot (insufficient/occupied history) -> %s",
            fallback.isoformat(),
        )
        return SlotRecommendation(
            scheduled_utc=fallback,
            source="fallback",
            day_of_week=local.weekday(),
            hour_local=local.hour,
            score=0.0,
            sample_size=0,
            reason="insufficient historical data or preferred slots occupied; configured fallback window",
        )

    # Last resort: never fail the approval — schedule at the lead time.
    last = earliest
    while _is_collision(last, occupied_utc, min_gap=min_gap):
        last += timedelta(minutes=cfg.min_gap_minutes)
    local = last.astimezone(MYT)
    logger.warning("smart_schedule: no free slot in window; using lead-time fallback %s", last.isoformat())
    return SlotRecommendation(
        scheduled_utc=last,
        source="fallback",
        day_of_week=local.weekday(),
        hour_local=local.hour,
        score=0.0,
        sample_size=0,
        reason="no free slot in lookahead window; scheduled at minimum lead time",
    )


def _next_occurrence(after_utc: datetime, dow: int, hour: int) -> datetime | None:
    """Next UTC datetime whose MYT local time falls on weekday ``dow`` at ``hour``."""
    local = after_utc.astimezone(MYT)
    days_ahead = (dow - local.weekday()) % 7
    candidate_local = datetime.combine(
        local.date() + timedelta(days=days_ahead), time(hour, 0), tzinfo=MYT
    )
    if candidate_local <= local:
        candidate_local += timedelta(days=7)
    return candidate_local.astimezone(timezone.utc)


def _fallback_slot(
    earliest: datetime,
    horizon: datetime,
    occupied: list[datetime],
    min_gap: timedelta,
    cfg: SchedulingConfig,
) -> datetime | None:
    local = earliest.astimezone(MYT)
    for day_offset in range(0, cfg.lookahead_days + 1):
        day = local.date() + timedelta(days=day_offset)
        for hour in sorted(cfg.fallback_hours_local):
            candidate_local = datetime.combine(day, time(hour, 0), tzinfo=MYT)
            candidate = candidate_local.astimezone(timezone.utc)
            if candidate < earliest or candidate > horizon:
                continue
            if not _is_collision(candidate, occupied, min_gap=min_gap):
                return candidate
    return None


# ---------------------------------------------------------------------------
# Custom time parsing (MYT-first)
# ---------------------------------------------------------------------------

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


class ScheduleParseError(ValueError):
    """Raised when a custom schedule string cannot be parsed."""


def parse_custom_time(text: str, *, now: datetime | None = None) -> datetime:
    """Parse a user-supplied schedule string into an aware UTC datetime.

    Accepted forms (case-insensitive, MYT unless an explicit offset is given):
      9pm                  -> today at 21:00 MYT (tomorrow if already past)
      21:00                -> today at 21:00 MYT (tomorrow if already past)
      tonight 9pm          -> today 21:00 MYT (even if slightly past, allow +6h grace)
      tomorrow 8:30pm      -> tomorrow 20:30 MYT
      25 Sep 9pm           -> that date, 21:00 MYT
      2026-09-25 21:00     -> ISO date, 21:00 MYT

    Raises ScheduleParseError on anything unrecognized or unparseable.
    """
    now = now or datetime.now(timezone.utc)
    now_local = now.astimezone(MYT)
    raw = (text or "").strip().lower()
    if not raw:
        raise ScheduleParseError("empty input")

    body = re.sub(r"\s+", " ", raw)
    day_offset = 0
    explicit_date: date | None = None

    # ISO date first: 2026-09-25
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", body)
    if m:
        try:
            explicit_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError as exc:
            raise ScheduleParseError(f"invalid date: {m.group(0)}") from exc
        body = (body[: m.start()] + " " + body[m.end():]).strip()
    else:
        # 25 Sep / 25 september
        m = re.search(r"\b(\d{1,2})\s+([a-z]{3,9})\b", body)
        if m and m.group(2)[:3] in _MONTHS:
            month = _MONTHS[m.group(2)[:3]]
            try:
                explicit_date = date(now_local.year, month, int(m.group(1)))
            except ValueError as exc:
                raise ScheduleParseError(f"invalid date: {m.group(0)}") from exc
            # If that date already passed this year and no time yet, assume next year.
            body = (body[: m.start()] + " " + body[m.end():]).strip()
        else:
            if "tonight" in body:
                day_offset = 0
                body = body.replace("tonight", " ").strip()
            elif "today" in body:
                day_offset = 0
                body = body.replace("today", " ").strip()
            elif "tomorrow" in body:
                day_offset = 1
                body = body.replace("tomorrow", " ").strip()

    # Time: 9pm / 9:30pm / 21:00 / 21
    hm = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", body)
    if not hm:
        raise ScheduleParseError(f"no time found in {text!r}")
    hour = int(hm.group(1))
    minute = int(hm.group(2) or 0)
    meridiem = hm.group(3)
    if meridiem:
        if hour == 12:
            hour = 0
        if meridiem == "pm":
            hour += 12
    if hour > 23 or minute > 59:
        raise ScheduleParseError(f"invalid time: {hm.group(0)}")

    if explicit_date is not None:
        target_date = explicit_date
        # If explicit date with no year already passed (and is today-past), roll forward.
        local_dt = datetime.combine(target_date, time(hour, minute), tzinfo=MYT)
        if m is None or not re.search(r"\b\d{4}-", raw):
            if local_dt < now_local and explicit_date <= now_local.date():
                # date-only (no year) in the past -> next year
                try:
                    target_date = date(target_date.year + 1, target_date.month, target_date.day)
                except ValueError:
                    target_date = target_date + timedelta(days=366)
    else:
        target_date = now_local.date() + timedelta(days=day_offset)

    local_dt = datetime.combine(target_date, time(hour, minute), tzinfo=MYT)

    # Bare time today that's already passed -> tomorrow (unless 'tonight' with grace).
    if explicit_date is None and day_offset == 0:
        if local_dt <= now_local:
            if "tonight" in raw and (now_local - local_dt) <= timedelta(hours=6):
                pass  # 'tonight 9pm' said at 9:20pm still means tonight
            else:
                local_dt += timedelta(days=1)

    if local_dt.astimezone(timezone.utc) <= now - timedelta(minutes=1):
        raise ScheduleParseError("time is in the past")
    return local_dt.astimezone(timezone.utc)
