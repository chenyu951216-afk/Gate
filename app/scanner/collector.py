import asyncio
from typing import Any

from app.gate.normalizer import closed_candles, normalize_candles
from app.gate.rest_client import GateRestClient


class MarketCollector:
    def __init__(self, client: GateRestClient, settings: Any, coinglass: Any | None = None):
        self.client = client
        self.settings = settings
        self.coinglass = coinglass

    def _coinglass_targets(self, universe: list[dict[str, Any]]) -> set[str]:
        limit = int(getattr(self.settings, "coinglass_max_symbols_per_scan", 0))
        if limit <= 0:
            return {str(item["info"].name).upper() for item in universe}
        ordered = sorted(
            universe,
            key=lambda item: float(item["ticker"].get("volume_24h_quote") or 0),
            reverse=True,
        )
        return {str(item["info"].name).upper() for item in ordered[:limit]}

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
        coinglass_targets = self._coinglass_targets(universe)

        async def one(item: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                try:
                    data = await self.collect_contract(item, as_of=as_of)
                    data["coinglass_required"] = bool(
                        getattr(self.settings, "coinglass_enabled", False)
                        and getattr(self.settings, "coinglass_required_for_ranking", True)
                    )
                    if self.coinglass and getattr(self.settings, "coinglass_enabled", False):
                        contract = str(item["info"].name).upper()
                        if contract in coinglass_targets:
                            data["coinglass"] = await self.coinglass.get_liquidation_features(
                                contract,
                                current_price=float(item["ticker"].get("mark_price") or item["ticker"].get("last") or 0),
                                as_of=as_of,
                            )
                        else:
                            data["coinglass"] = {
                                "available": False,
                                "errors": ["coinglass_scan_budget_exceeded"],
                                "symbol": contract.split("_")[0],
                            }
                    return data
                except Exception as exc:
                    return {
                        "info": item["info"],
                        "ticker": item["ticker"],
                        "snapshot": item["snapshot"],
                        "coinglass_required": bool(
                            getattr(self.settings, "coinglass_enabled", False)
                            and getattr(self.settings, "coinglass_required_for_ranking", True)
                        ),
                        "coinglass": {"available": False, "errors": [type(exc).__name__]},
                        "collection_errors": [type(exc).__name__],
                    }

        return await asyncio.gather(*(one(item) for item in universe))
