import asyncio
from typing import Any

from app.gate.rest_client import GateRestClient
from app.gate.normalizer import closed_candles, normalize_candles
from app.scanner.integrity import candle_integrity


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

        requested = {
            "4h": max(self.settings.min_4h_candles, 240),
            "30m": max(self.settings.min_30m_candles, 500),
            "15m": 240,
            "5m": 240,
        }
        required = {
            "4h": int(self.settings.min_4h_candles),
            "30m": int(self.settings.min_30m_candles),
            "15m": 50,
            "5m": 50,
        }
        results = await asyncio.gather(
            candles("4h", requested["4h"]),
            candles("30m", requested["30m"]),
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
        data["market_session"] = universe_item.get("market_session", {})
        data["collection_errors"] = [f"{key}:{type(value).__name__}" for key, value in zip(keys, results, strict=True) if isinstance(value, Exception)]
        integrity: dict[str, dict[str, Any]] = {}
        for interval in ("4h", "30m", "15m", "5m"):
            report = candle_integrity(
                data[interval],
                interval,
                required[interval],
                as_of=as_of,
            )
            if not report["complete"]:
                # A second, wider read repairs transient pagination overlap or
                # an incomplete first response. It is only paid for on a bad
                # series, not for every contract on every scan.
                try:
                    repaired = await candles(interval, requested[interval] + 100)
                    repaired_report = candle_integrity(
                        repaired,
                        interval,
                        required[interval],
                        as_of=as_of,
                    )
                    if repaired_report["complete"] or len(repaired) > len(
                        data[interval]
                    ):
                        data[interval] = repaired
                        report = repaired_report
                except Exception as exc:
                    data["collection_errors"].append(
                        f"{interval}_repair:{type(exc).__name__}"
                    )
            integrity[interval] = report
        data["data_integrity"] = integrity
        primary_problems = {
            problem
            for interval in ("4h", "30m")
            for problem in integrity[interval]["problems"]
        }
        data["collection_errors"].extend(sorted(primary_problems))
        # Only the thesis frames are fatal. Optional OI/funding/trade failures
        # remain visible and reduce completeness, but must not suppress an
        # otherwise complete 4h/30m trend.
        if any(isinstance(value, Exception) for value in results[:2]):
            data["collection_errors"].append("api_partial_failure")
        data["collection_errors"] = sorted(set(data["collection_errors"]))
        return data

    async def collect_batch(self, universe: list[dict[str, Any]], as_of=None) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(int(self.settings.gate_max_concurrency))
        coinglass_targets = self._coinglass_targets(universe)

        async def one(item: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                try:
                    data = await self.collect_contract(item, as_of=as_of)
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
                        "coinglass": {"available": False, "errors": [type(exc).__name__]},
                        "collection_errors": [type(exc).__name__],
                    }

        return await asyncio.gather(*(one(item) for item in universe))
