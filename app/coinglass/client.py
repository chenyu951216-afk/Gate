import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

import httpx

from app.gate.rate_limiter import AsyncRateLimiter

logger = logging.getLogger(__name__)


class CoinGlassAPIError(RuntimeError):
    """Raised when CoinGlass cannot provide a valid response."""


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _symbol(contract: str) -> str:
    return str(contract).upper().replace("-", "_").split("_")[0]


def _interval_seconds(interval: str) -> int:
    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    text = str(interval).strip().lower()
    for suffix, multiplier in units.items():
        if text.endswith(suffix):
            try:
                return max(60, int(text[:-1]) * multiplier)
            except ValueError:
                break
    return 1800


def _history_features(payload: Any) -> dict[str, Any]:
    rows = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise CoinGlassAPIError("CoinGlass liquidation history has invalid data")
    normalized: list[dict[str, float]] = []
    long_total = 0.0
    short_total = 0.0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        timestamp = _number(row.get("time"))
        long_value = _number(row.get("aggregated_long_liquidation_usd")) or 0.0
        short_value = _number(row.get("aggregated_short_liquidation_usd")) or 0.0
        long_total += max(0.0, long_value)
        short_total += max(0.0, short_value)
        normalized.append(
            {
                "time": timestamp or 0.0,
                "long_usd": max(0.0, long_value),
                "short_usd": max(0.0, short_value),
            }
        )
    total = long_total + short_total
    bias = (short_total - long_total) / total if total else 0.0
    dominant = "balanced"
    if bias >= 0.2:
        dominant = "short_liquidation_dominant"
    elif bias <= -0.2:
        dominant = "long_liquidation_dominant"
    return {
        "rows": normalized,
        "long_usd": long_total,
        "short_usd": short_total,
        "total_usd": total,
        "directional_bias": bias,
        "dominant": dominant,
    }


def _heatmap_features(payload: Any, current_price: float | None) -> dict[str, Any]:
    root = payload.get("data") if isinstance(payload, Mapping) else None
    if isinstance(root, Mapping) and isinstance(root.get("data"), Mapping):
        root = root["data"]
    if not isinstance(root, Mapping):
        raise CoinGlassAPIError("CoinGlass liquidation heatmap has invalid data")
    levels: list[dict[str, float]] = []
    for key, value in root.items():
        price = _number(key)
        entries = value if isinstance(value, list) else [value]
        if price is None:
            continue
        for entry in entries:
            if isinstance(entry, list) and len(entry) >= 2:
                level_price = _number(entry[0]) or price
                usd = _number(entry[1])
            elif isinstance(entry, Mapping):
                level_price = _number(entry.get("price")) or price
                usd = _number(entry.get("usd_value") or entry.get("value") or entry.get("amount"))
            else:
                level_price = price
                usd = _number(entry)
            if usd is not None and usd > 0:
                levels.append({"price": level_price, "usd": usd})
    levels.sort(key=lambda item: item["price"])
    below = [item for item in levels if current_price and item["price"] < current_price]
    above = [item for item in levels if current_price and item["price"] > current_price]
    strongest = sorted(levels, key=lambda item: item["usd"], reverse=True)[:20]
    return {
        "levels": levels,
        "strongest_levels": strongest,
        "cluster_count": len(levels),
        "cluster_total_usd": sum(item["usd"] for item in levels),
        "nearest_below": max(below, key=lambda item: item["price"], default=None),
        "nearest_above": min(above, key=lambda item: item["price"], default=None),
    }


class CoinGlassClient:
    """Small, rate-limited CoinGlass v4 client with per-scan caching.

    CoinGlass is an analysis input only. It never places or changes orders.
    """

    HISTORY_PATH = "/api/futures/liquidation/aggregated-history"
    HEATMAP_PATH = "/api/futures/liquidation/aggregated-map"

    def __init__(self, settings: Any, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.coinglass_base_url.rstrip("/"),
            timeout=settings.coinglass_request_timeout_seconds,
            transport=transport,
            headers={"Accept": "application/json"},
        )
        self.limiter = AsyncRateLimiter(settings.coinglass_requests_per_second)
        self.semaphore = asyncio.Semaphore(settings.coinglass_max_concurrency)
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._cache_lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(self, path: str, params: dict[str, Any]) -> Any:
        if not self.settings.coinglass_api_key:
            raise CoinGlassAPIError("COINGLASS_API_KEY is not configured")
        attempts = max(1, int(self.settings.coinglass_retry_attempts))
        headers = {"CG-API-KEY": self.settings.coinglass_api_key}
        async with self.semaphore:
            for attempt in range(attempts):
                await self.limiter.acquire()
                try:
                    response = await self.client.get(path, params=params, headers=headers)
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt + 1 == attempts:
                            response.raise_for_status()
                        await asyncio.sleep(min(8.0, 0.5 * 2**attempt))
                        continue
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, Mapping) or str(payload.get("code", "0")) not in {"0", "0.0"}:
                        raise CoinGlassAPIError(str(payload.get("msg", "CoinGlass returned an error")))
                    return payload
                except httpx.HTTPStatusError as exc:
                    detail = exc.response.text[:300]
                    raise CoinGlassAPIError(f"CoinGlass HTTP {exc.response.status_code}: {detail}") from exc
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if attempt + 1 == attempts:
                        raise CoinGlassAPIError(f"CoinGlass network failure: {type(exc).__name__}") from exc
                    await asyncio.sleep(min(8.0, 0.5 * 2**attempt))
        raise CoinGlassAPIError("CoinGlass request failed")

    async def get_liquidation_features(
        self, contract: str, current_price: float | None = None, as_of: Any | None = None
    ) -> dict[str, Any]:
        symbol = _symbol(contract)
        if not self.settings.coinglass_enabled:
            return {"available": False, "errors": ["coinglass_disabled"], "symbol": symbol}
        if not self.settings.coinglass_api_key:
            return {"available": False, "errors": ["coinglass_api_key_missing"], "symbol": symbol}
        if as_of is not None:
            end_ms = int(as_of.timestamp() * 1000)
            cache_key = f"{symbol}:{end_ms // 1_800_000}"
        else:
            end_ms = int(time.time() * 1000)
            cache_key = symbol
        async with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < int(self.settings.coinglass_cache_ttl_seconds):
                return cached[1]
        interval = str(self.settings.coinglass_interval)
        history_params = {
            "exchange_list": str(self.settings.coinglass_exchange_list),
            "symbol": symbol,
            "interval": interval,
            "limit": min(1000, int(self.settings.coinglass_history_limit)),
            "end_time": end_ms,
        }
        start_ms = end_ms - _interval_seconds(interval) * int(self.settings.coinglass_history_limit) * 1000
        history_params["start_time"] = start_ms
        history_task = self._request(self.HISTORY_PATH, history_params)
        heatmap_task = None
        if self.settings.coinglass_use_heatmap:
            heatmap_task = self._request(
                self.HEATMAP_PATH,
                {"symbol": symbol, "range": str(getattr(self.settings, "coinglass_heatmap_range", "1d"))},
            )
        history_result: Any
        heatmap_result: Any
        history_result, heatmap_result = await asyncio.gather(
            history_task,
            heatmap_task if heatmap_task is not None else asyncio.sleep(0, result=None),
            return_exceptions=True,
        )
        result: dict[str, Any] = {
            "available": False,
            "history_available": False,
            "heatmap_available": False,
            "symbol": symbol,
            "errors": [],
        }
        if isinstance(history_result, Exception):
            result["errors"].append(f"history:{type(history_result).__name__}")
        else:
            try:
                result["liquidation"] = _history_features(history_result)
                result["history_available"] = True
            except Exception as exc:
                result["errors"].append(f"history_parse:{type(exc).__name__}")
        if heatmap_task is not None:
            if isinstance(heatmap_result, Exception):
                result["errors"].append(f"heatmap:{type(heatmap_result).__name__}")
            else:
                try:
                    result["heatmap"] = _heatmap_features(heatmap_result, current_price)
                    result["heatmap_available"] = True
                except Exception as exc:
                    result["errors"].append(f"heatmap_parse:{type(exc).__name__}")
        result["available"] = bool(result["history_available"] and (not self.settings.coinglass_require_heatmap or result["heatmap_available"]))
        if result["available"]:
            result["errors"] = []
        async with self._cache_lock:
            self._cache[cache_key] = (time.monotonic(), result)
        return result
