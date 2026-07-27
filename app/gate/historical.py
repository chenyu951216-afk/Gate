import asyncio
from datetime import datetime, timezone
from typing import Any

from app.gate.normalizer import normalize_candles
from app.gate.rest_client import GateRestClient


async def fetch_warmup_candles(
    client: GateRestClient,
    contract: str,
    interval: str,
    end_time: datetime,
    required: int,
    quanto_multiplier: float | None = None,
) -> list[Any]:
    end_ts = int(end_time.astimezone(timezone.utc).timestamp())
    raw = await client.get_candlesticks(contract, interval, limit=min(required, 2000), to_ts=end_ts)
    return normalize_candles(raw, quanto_multiplier=quanto_multiplier)


async def collect_historical_bundle(
    client: GateRestClient,
    contract: str,
    end_time: datetime,
    requirements: dict[str, int],
    quanto_multiplier: float | None = None,
) -> dict[str, Any]:
    intervals = await asyncio.gather(
        *[
            fetch_warmup_candles(client, contract, interval, end_time, count, quanto_multiplier)
            for interval, count in requirements.items()
        ],
        return_exceptions=True,
    )
    bundle: dict[str, Any] = {}
    for interval, value in zip(requirements, intervals, strict=True):
        bundle[interval] = [] if isinstance(value, Exception) else value
    return bundle

