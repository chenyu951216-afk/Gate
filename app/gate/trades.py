from datetime import datetime, timezone
from typing import Any


def aggregate_taker_flow(rows: list[dict[str, Any]], as_of: datetime | None = None) -> dict[str, Any]:
    """Aggregate Gate trade size; positive size is taker buy and negative size is taker sell."""
    reference = as_of or datetime.now(timezone.utc)
    buy = 0.0
    sell = 0.0
    latest_timestamp: datetime | None = None
    for row in rows:
        try:
            timestamp = datetime.fromtimestamp(float(row.get("create_time_ms", row.get("create_time", 0))) / (1000 if row.get("create_time_ms") else 1), tz=timezone.utc)
            raw_size = row.get("size")
            if raw_size is None:
                continue
            size = float(raw_size)
        except (TypeError, ValueError, OverflowError):
            continue
        if timestamp > reference:
            continue
        latest_timestamp = max(latest_timestamp, timestamp) if latest_timestamp else timestamp
        if size >= 0:
            buy += size
        else:
            sell += abs(size)
    available = buy > 0 or sell > 0
    return {
        "active_buy_volume": buy if available else None,
        "active_sell_volume": sell if available else None,
        "buy_sell_ratio": buy / sell if sell else None,
        "sell_buy_ratio": sell / buy if buy else None,
        "available": available,
        "latest_timestamp": latest_timestamp,
    }
