"""One-shot historical Threads Insights collector."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .insights import sample_interval_minutes
from .threads_api import ACCOUNT_METRICS, POST_METRICS


def collect_once(
    api: Any,
    store: Any,
    now: datetime | None = None,
    *,
    account_sample_minutes: int = 15,
    post_limit: int = 100,
) -> dict[str, Any]:
    """Collect due account and post snapshots once.

    Raw rows are only appended. Per-post Insight failures are isolated so one
    unavailable metric endpoint does not discard snapshots for other posts.
    """
    now = _ensure_aware(now or datetime.now(timezone.utc))
    captured_at = now.isoformat()
    summary: dict[str, Any] = {
        "captured_at": captured_at,
        "account_snapshot": "skipped",
        "account_failure": None,
        "posts_sampled": 0,
        "posts_skipped": 0,
        "post_failures": [],
    }

    _collect_account_if_due(
        api,
        store,
        now,
        captured_at,
        account_sample_minutes,
        summary,
    )

    posts = api.list_posts(limit=post_limit)
    for post in posts:
        post_id = post.get("id")
        if not post_id:
            summary["posts_skipped"] += 1
            continue

        published_at = _parse_datetime(post.get("timestamp"))
        age_minutes = _age_minutes(now, published_at)
        interval = sample_interval_minutes(age_minutes)
        latest = store.latest_post_snapshot(post_id)
        if latest and not _is_due(now, latest.get("captured_at"), interval):
            summary["posts_skipped"] += 1
            continue

        try:
            metrics = api.get_post_insights(post_id)
            payload: dict[str, Any] = {
                "captured_at": captured_at,
                "account_id": api.user_id,
                "post_id": post_id,
                "published_at": published_at.isoformat() if published_at else None,
                "post_age_minutes": round(age_minutes, 3),
            }
            for name in POST_METRICS:
                payload[name] = metrics.get(name)
            store.insert_post_snapshot(payload)
            summary["posts_sampled"] += 1
        except Exception as exc:  # isolate one post from the rest of the run
            summary["post_failures"].append(
                {"post_id": post_id, "error": f"{type(exc).__name__}: {exc}"}
            )

    return summary


def _collect_account_if_due(
    api: Any,
    store: Any,
    now: datetime,
    captured_at: str,
    interval_minutes: int,
    summary: dict[str, Any],
) -> None:
    latest = store.latest_account_snapshot(api.user_id)
    if latest and not _is_due(now, latest.get("captured_at"), interval_minutes):
        return
    try:
        metrics = api.get_account_insights()
        payload: dict[str, Any] = {
            "captured_at": captured_at,
            "account_id": api.user_id,
        }
        for name in ACCOUNT_METRICS:
            payload[name] = metrics.get(name)
        store.insert_account_snapshot(payload)
        summary["account_snapshot"] = "stored"
    except Exception as exc:
        summary["account_snapshot"] = "failed"
        summary["account_failure"] = f"{type(exc).__name__}: {exc}"


def _is_due(now: datetime, captured_at: str | None, interval_minutes: int) -> bool:
    if not captured_at:
        return True
    previous = _parse_datetime(captured_at)
    if previous is None:
        return True
    elapsed = (now - previous).total_seconds() / 60.0
    return elapsed >= max(1, interval_minutes)


def _age_minutes(now: datetime, published_at: datetime | None) -> float:
    if published_at is None:
        return 0.0
    return max(0.0, (now - published_at).total_seconds() / 60.0)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return _ensure_aware(datetime.fromisoformat(normalized))
    except ValueError:
        return None


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
