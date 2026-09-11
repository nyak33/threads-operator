"""Pure analytics primitives for historical Threads Insights snapshots."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

MEASURED: Final[str] = "measured"
CALCULATED: Final[str] = "calculated"
INFERRED: Final[str] = "inferred"
ATTRIBUTED: Final[str] = "attributed"


def sample_interval_minutes(post_age_minutes: float) -> int:
    """Return the minimum snapshot interval for a post of the given age."""
    if post_age_minutes < 0:
        post_age_minutes = 0
    if post_age_minutes < 120:
        return 5
    if post_age_minutes < 360:
        return 15
    if post_age_minutes < 1440:
        return 30
    if post_age_minutes < 4320:
        return 60
    if post_age_minutes < 10080:
        return 360
    return 1440


def growth_rate(
    previous: int | float | None,
    current: int | float | None,
) -> float | None:
    """Return percentage growth, or None when the baseline is unknown/zero."""
    if previous is None or current is None or previous == 0:
        return None
    return (float(current) - float(previous)) / float(previous) * 100.0


def velocity(
    previous_value: int | float | None,
    current_value: int | float | None,
    elapsed_minutes: int | float,
) -> float | None:
    """Return units per minute for two cumulative measurements."""
    if previous_value is None or current_value is None or elapsed_minutes <= 0:
        return None
    return (float(current_value) - float(previous_value)) / float(elapsed_minutes)


def detect_second_wave(
    points: Sequence[tuple[int | float, int | float | None]],
    min_acceleration_ratio: float = 2.0,
) -> bool:
    """Detect slowdown followed by material re-acceleration in cumulative values.

    This is an internal pattern label, not evidence of an official Meta
    distribution event. Points are ``(elapsed_minutes, cumulative_value)``.
    """
    if len(points) < 4 or min_acceleration_ratio <= 1:
        return False

    interval_velocities: list[float] = []
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if v0 is None or v1 is None or t1 <= t0 or v1 < v0:
            return False
        rate = velocity(v0, v1, t1 - t0)
        if rate is None:
            return False
        interval_velocities.append(rate)

    if len(interval_velocities) < 3:
        return False

    for trough_index in range(1, len(interval_velocities) - 1):
        trough = interval_velocities[trough_index]
        prior_peak = max(interval_velocities[:trough_index])
        later_peak = max(interval_velocities[trough_index + 1 :])
        if prior_peak <= 0:
            continue
        slowed_materially = trough <= prior_peak * 0.5
        reaccelerated = (
            later_peak > trough
            and later_peak >= max(trough * min_acceleration_ratio, prior_peak * 0.25)
        )
        if slowed_materially and reaccelerated:
            return True
    return False
