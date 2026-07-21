import asyncio
from datetime import datetime, timezone
from typing import Any

from app.gate.normalizer import closed_candles, normalize_candles
from app.gate.rest_client import GateRestClient
from app.replay.validation import contract_existed


class HistoricalCollector:
    def __init__(self, gate: GateRestClient, settings: Any):
        self.gate = gate
        self.settings = settings
        self.cache: dict[tuple[str, str, int], list[Any]] = {}

    async def collect_universe(self, as_of: datetime) -> list[dict[str, Any]]:
        contracts = await self.gate.get_contracts(include_delisted=True)
        return [item for item in contracts if contract_existed(item, as_of) and item.get("status") in {"trading", "delisted", "delisting"}]

    async def collect_contract(self, raw: dict[str, Any], as_of: datetime, include_details: bool = True) -> dict[str, Any]:
        contract = str(raw["name"])
        as_of_ts = int(as_of.astimezone(timezone.utc).timestamp())
        multiplier = float(raw["quanto_multiplier"]) if raw.get("quanto_multiplier") not in (None, "") else None

        async def get_candles(interval: str, required: int) -> list[Any]:
            cache_key = (contract, interval, as_of_ts)
            if cache_key in self.cache:
                return self.cache[cache_key]
            response = await self.gate.get_candlesticks(contract, interval, limit=min(required, 2000), to_ts=as_of_ts)
            candles = closed_candles(normalize_candles(response, multiplier), interval, as_of=as_of)
            self.cache[cache_key] = candles
            return candles

        results = await asyncio.gather(
            get_candles("4h", 240), get_candles("30m", 500), get_candles("15m", 240), get_candles("5m", 240),
            self.gate.get_contract_stats(contract, from_ts=as_of_ts - 30 * 86400, limit=1000),
            self.gate.get_funding_rates(contract, to_ts=as_of_ts, limit=200),
            self.gate.get_trades(contract, limit=1000),
            return_exceptions=True,
        )
        names = ("4h", "30m", "15m", "5m", "oi", "funding", "trades")
        data: dict[str, Any] = {name: ([] if isinstance(value, BaseException) else value) for name, value in zip(names, results, strict=True)}
        data["info_raw"] = raw
        data["historical_unavailable"] = ["spread", "historical_24h_ticker", "active_trade_aggregate"]
        data["collection_errors"] = [f"{name}:{type(value).__name__}" for name, value in zip(names, results, strict=True) if isinstance(value, Exception)]
        return data
