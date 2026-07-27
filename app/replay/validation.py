from datetime import datetime
from typing import Any


def candle_diagnostics(candles: list[Any], interval_seconds: int, as_of: datetime) -> list[str]:
    problems: list[str] = []
    timestamps = [item.timestamp for item in candles]
    if timestamps != sorted(set(timestamps)):
        problems.append("duplicate_or_unsorted_candles")
    if any(timestamp.timestamp() + interval_seconds > as_of.timestamp() for timestamp in timestamps):
        problems.append("future_candle")
    for previous, current in zip(timestamps, timestamps[1:]):
        if (current - previous).total_seconds() > interval_seconds * 1.5:
            problems.append("candle_gap")
            break
    return problems


def contract_existed(raw: dict[str, Any], as_of: datetime) -> bool:
    launch = raw.get("launch_time")
    delisted = raw.get("delisted_time") or raw.get("delisting_time")
    timestamp = as_of.timestamp()
    return (launch is None or float(launch) <= timestamp) and (delisted is None or float(delisted) > timestamp)

