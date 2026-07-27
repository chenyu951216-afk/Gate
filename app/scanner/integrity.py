from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.constants import INTERVAL_SECONDS


def candle_integrity(
    candles: list[Any],
    interval: str,
    minimum: int,
    *,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Validate count, uniqueness, alignment, continuity and freshness."""
    seconds = INTERVAL_SECONDS[interval]
    reference = as_of or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    timestamps = [item.timestamp.astimezone(timezone.utc) for item in candles]
    problems: list[str] = []
    if len(timestamps) < minimum:
        problems.append("insufficient_history")
    if len(set(timestamps)) != len(timestamps):
        problems.append("duplicate_timestamp")
    if any(int(value.timestamp()) % seconds != 0 for value in timestamps):
        problems.append("time_alignment_error")

    window = timestamps[-max(2, minimum) :]
    gaps = [
        {
            "after": previous.isoformat(),
            "before": current.isoformat(),
            "seconds": int((current - previous).total_seconds()),
        }
        for previous, current in zip(window, window[1:], strict=False)
        if int((current - previous).total_seconds()) != seconds
    ]
    if gaps:
        problems.append("historical_data_gap")

    stale_seconds = None
    if timestamps:
        latest_close = timestamps[-1] + timedelta(seconds=seconds)
        stale_seconds = (reference - latest_close).total_seconds()
        if stale_seconds < -1:
            problems.append("time_alignment_error")
        elif stale_seconds > seconds + 120:
            problems.append("stale_data")
    else:
        problems.append("insufficient_history")

    return {
        "interval": interval,
        "complete": not problems,
        "count": len(timestamps),
        "required": minimum,
        "problems": sorted(set(problems)),
        "gap_count": len(gaps),
        "gaps": gaps[:10],
        "stale_seconds": stale_seconds,
        "first_timestamp": timestamps[0].isoformat() if timestamps else None,
        "last_timestamp": timestamps[-1].isoformat() if timestamps else None,
    }
