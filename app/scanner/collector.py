import asyncio
from typing import Any

from app.gate.normalizer import closed_candles, normalize_candles
from app.gate.rest_client import GateRestClient


class MarketCollector:
    def __init__(self, client: GateRestClient, settings: Any):
        self.client = client
        self.settings = settings

    async def collect_contract(self, universe_item: dict[str, Any], as_of=None) -> dict[str, Any]:
        info = universe_item["info"]
        contract = info.name
        end_ts = int(as_of.timestamp()) if as_of is not None else None
        async def candles(interval: str, count: int) -> list[Any]:
            raw = await self.client.get_candlesticks(contract, interval, limit=count, to_ts=end_ts)
            return closed_candles(normalize_candles(raw, info.quanto_multiplier), interval, as_of=as_of)

        results = await asyncio.gather(
            candles("4h", max(self.settings.min_4h_candles, 240)),
            candles("30m", max(self.settings.min_30m_candles, 500)),
            candles("15m", 240),
            candles("5m", 240),
            self.client.get_contract_stats(contract, from_ts=(end_ts - 30 * 86400 if end_ts else None), limit=1000),
            self.client.get_funding_rates(contract, to_ts=end_ts, limit=200),
            self.client.get_trades(contract, limit=1000),
            return_exceptions=True,
        )
        keys = ("4h", "30m", "15m", "5m", "oi", "funding", "trades")
        data: dict[str, Any] = {key: ([] if isinstance(value, Exception) else value) for key, value in zip(keys, results, strict=True)}
        data["info"] = info
        data["ticker"] = universe_item["ticker"]
        data["snapshot"] = universe_item["snapshot"]
        data["collection_errors"] = [f"{key}:{type(value).__name__}" for key, value in zip(keys, results, strict=True) if isinstance(value, Exception)]
        return data

    async def collect_batch(self, universe: list[dict[str, Any]], as_of=None) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(self.settings.gate_max_concurrency)

        async def one(item: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                try:
                    return await self.collect_contract(item, as_of=as_of)
                except Exception as exc:
                    return {"info": item["info"], "ticker": item["ticker"], "snapshot": item["snapshot"], "collection_errors": [type(exc).__name__]}

        return await asyncio.gather(*(one(item) for item in universe))
