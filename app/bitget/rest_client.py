import asyncio
import base64
import hashlib
import hmac
import json
import logging
import random
import time
from collections.abc import Mapping
from decimal import Decimal, ROUND_DOWN
from typing import Any

import httpx

from app.exceptions import GateAPIError
from app.gate.rate_limiter import AsyncRateLimiter

logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    result = format(Decimal(str(value)), "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result or "0"


def _positive(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def _tick_from_places(value: Any, fallback: float = 0.00000001) -> float:
    try:
        return 10 ** (-int(value))
    except (TypeError, ValueError):
        return fallback


class CircuitBreaker:
    def __init__(self, threshold: int, recovery_seconds: float):
        self.threshold = threshold
        self.recovery_seconds = recovery_seconds
        self.failures = 0
        self.opened_at: float | None = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at >= self.recovery_seconds:
            self.opened_at = None
            self.failures = 0
            return True
        return False

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()


class BitgetRestClient:
    """Official Bitget v2 REST client with the application's legacy shape."""

    def __init__(self, settings: Any, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.product_type = str(getattr(settings, "bitget_product_type", "USDT-FUTURES")).upper()
        self.margin_coin = str(getattr(settings, "bitget_margin_coin", "USDT")).upper()
        self.client = httpx.AsyncClient(
            base_url=str(settings.bitget_rest_base_url).rstrip("/"),
            timeout=float(settings.bitget_request_timeout_seconds),
            transport=transport,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        self.limiter = AsyncRateLimiter(float(settings.bitget_requests_per_second))
        self.semaphore = asyncio.Semaphore(int(settings.bitget_max_concurrency))
        self.breaker = CircuitBreaker(
            int(settings.bitget_circuit_failure_threshold),
            float(settings.bitget_circuit_recovery_seconds),
        )
        self._contracts: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        await self.client.aclose()

    @staticmethod
    def _symbol(contract: str) -> str:
        value = str(contract).upper().replace("-", "_")
        if value.endswith("_USDT"):
            return value[:-5] + "USDT"
        if value.endswith("USDT"):
            return value
        return value.replace("_", "")

    @staticmethod
    def _contract(symbol: str) -> str:
        value = str(symbol).upper()
        return value[:-4] + "_USDT" if value.endswith("USDT") else value

    @staticmethod
    def contract_identity(value: str) -> tuple[str, str] | None:
        """Return an exact base/quote identity for cross-venue validation.

        This intentionally does not perform fuzzy symbol matching.  Prefixes
        such as ``1000`` are part of the base asset and therefore remain part
        of the identity; a missing or different base is not tradable.
        """
        normalized = str(value or "").upper().replace("-", "_").replace("/", "_")
        if normalized.endswith("_USDT"):
            return normalized[:-5], "USDT"
        if normalized.endswith("USDT"):
            return normalized[:-4], "USDT"
        return None

    def _path(self, endpoint: str) -> str:
        return f"/api/v2{endpoint}"

    def _auth_headers(self, method: str, path: str, query: str, body: str) -> dict[str, str]:
        key = getattr(self.settings, "bitget_api_key", None)
        secret = getattr(self.settings, "bitget_api_secret", None)
        passphrase = getattr(self.settings, "bitget_api_passphrase", None)
        if not key or not secret or not passphrase:
            raise GateAPIError("Bitget API key, secret and passphrase are required", endpoint=path)
        timestamp = str(int(time.time() * 1000))
        request_path = path + (f"?{query}" if query else "")
        prehash = timestamp + method.upper() + request_path + body
        signature = base64.b64encode(
            hmac.new(str(secret).encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        return {
            "ACCESS-KEY": str(key),
            "ACCESS-SIGN": signature,
            "ACCESS-PASSPHRASE": str(passphrase),
            "ACCESS-TIMESTAMP": timestamp,
            "locale": "en-US",
        }

    async def request(
        self,
        method: str,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        authenticated: bool = False,
        json_body: Any | None = None,
    ) -> Any:
        path = self._path(endpoint)
        if not self.breaker.allow():
            raise GateAPIError("Bitget circuit breaker is open", endpoint=path)
        query = str(httpx.QueryParams({k: v for k, v in (params or {}).items() if v is not None}))
        body = "" if json_body is None else json.dumps(json_body, separators=(",", ":"), ensure_ascii=False)
        headers = self._auth_headers(method, path, query, body) if authenticated else {}
        attempts = max(1, int(self.settings.bitget_retry_attempts))
        async with self.semaphore:
            for attempt in range(attempts):
                await self.limiter.acquire()
                try:
                    response = await self.client.request(
                        method,
                        path,
                        params=params,
                        headers=headers,
                        content=body if json_body is not None else None,
                    )
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt + 1 == attempts:
                            response.raise_for_status()
                        retry_after = float(response.headers.get("Retry-After", "0") or 0)
                        await asyncio.sleep(retry_after or min(8.0, 0.5 * 2**attempt) + random.uniform(0, 0.2))
                        continue
                    response.raise_for_status()
                    try:
                        payload = response.json()
                    except json.JSONDecodeError as exc:
                        raise GateAPIError("Bitget returned invalid JSON", endpoint=path) from exc
                    if not isinstance(payload, Mapping) or str(payload.get("code")) != "00000":
                        code = payload.get("code") if isinstance(payload, Mapping) else "unknown"
                        message = payload.get("msg") if isinstance(payload, Mapping) else str(payload)
                        raise GateAPIError(f"Bitget API error {code}: {message}", endpoint=path)
                    self.breaker.success()
                    return payload.get("data")
                except httpx.HTTPStatusError as exc:
                    self.breaker.failure()
                    detail = exc.response.text[:500]
                    if exc.response.status_code >= 500 and attempt + 1 < attempts:
                        await asyncio.sleep(min(8.0, 0.5 * 2**attempt) + random.uniform(0, 0.2))
                        continue
                    raise GateAPIError(
                        f"Bitget HTTP error {exc.response.status_code}: {detail}",
                        status_code=exc.response.status_code,
                        endpoint=path,
                    ) from exc
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    self.breaker.failure()
                    if attempt + 1 == attempts:
                        raise GateAPIError(f"Bitget network failure: {type(exc).__name__}", endpoint=path) from exc
                    await asyncio.sleep(min(8.0, 0.5 * 2**attempt) + random.uniform(0, 0.2))
                except GateAPIError:
                    self.breaker.failure()
                    raise
        raise GateAPIError("Bitget request failed", endpoint=path)

    def _normalize_contract(self, raw: dict[str, Any]) -> dict[str, Any]:
        symbol = str(raw.get("symbol", "")).upper()
        status = str(raw.get("symbolStatus", "")).lower()
        # Bitget exposes `priceEndStep` as the number of units in the last
        # decimal place, not as an absolute price increment.  For example,
        # priceEndStep=1 and pricePlace=4 means a tick size of 0.0001.
        # Treating the raw value as 1.0 turns every sub-unit price into zero
        # after rounding and causes INVALID_LIMIT_PRICE before submission.
        price_step = _positive(raw.get("priceEndStep"), 1.0) * _tick_from_places(raw.get("pricePlace"))
        size_step = _positive(raw.get("sizeMultiplier"), _positive(raw.get("minTradeNum"), 0.00000001))
        result = {
            "name": self._contract(symbol),
            "status": "trading" if status == "normal" else "delisted",
            "type": "direct",
            "quanto_multiplier": 1,
            "contract_size": 1,
            "leverage_min": _positive(raw.get("minLever"), 1),
            "leverage_max": _positive(raw.get("maxLever"), 1),
            "order_price_round": price_step,
            "mark_price_round": _tick_from_places(raw.get("pricePlace"), price_step),
            "order_size_min": _positive(raw.get("minTradeNum"), size_step),
            "order_size_max": _positive(raw.get("maxOrderQty"), _positive(raw.get("maxMarketOrderQty"), 0)),
            "enable_decimal": True,
            # Keep the official Bitget fields at the normalized top level as
            # well as under `raw`.  The shared execution-risk code uses these
            # fields to round base-coin quantities correctly.
            "symbol": symbol,
            "baseCoin": raw.get("baseCoin"),
            "quoteCoin": raw.get("quoteCoin"),
            "symbolStatus": raw.get("symbolStatus"),
            "sizeMultiplier": raw.get("sizeMultiplier"),
            "minTradeNum": raw.get("minTradeNum"),
            "maxOrderQty": raw.get("maxOrderQty"),
            "priceEndStep": raw.get("priceEndStep"),
            "pricePlace": raw.get("pricePlace"),
            "raw": raw,
        }
        return result

    async def get_contracts(self, include_delisted: bool = False) -> list[dict[str, Any]]:
        payload = await self.request(
            "GET", "/mix/market/contracts", params={"productType": self.product_type}
        )
        rows = payload if isinstance(payload, list) else []
        result = [self._normalize_contract(dict(item)) for item in rows if isinstance(item, Mapping)]
        self._contracts = {item["name"]: item for item in result}
        return result if include_delisted else [item for item in result if item["status"] == "trading"]

    async def resolve_contract(self, source_contract: str, contracts: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """Resolve a Gate candidate to one exact Bitget USDT perpetual.

        The scanner is allowed to produce candidates only from Gate.  This
        method is a second exchange-side check before an order is sent and
        returns no match when the base asset cannot be proven identical.
        """
        source_identity = self.contract_identity(source_contract)
        if source_identity is None:
            return None
        available = contracts if contracts is not None else await self.get_contracts()
        matches = [
            item
            for item in available
            if self.contract_identity(str(item.get("name", ""))) == source_identity
            and str(item.get("status", "")).lower() == "trading"
            and str(item.get("raw", {}).get("symbolType", "perpetual")).lower() == "perpetual"
        ]
        return matches[0] if len(matches) == 1 else None

    def _ticker(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        symbol = str(raw.get("symbol", "")).upper()
        last = raw.get("lastPr")
        open_price = raw.get("open24h")
        change = raw.get("change24h")
        try:
            last_number = float(str(last))
            open_number = float(str(open_price))
            if change in (None, "") and open_number:
                change = (last_number - open_number) / open_number * 100
            elif change not in (None, ""):
                change_number = float(str(change))
                change = change_number * 100 if abs(change_number) < 1 else change_number
        except (TypeError, ValueError, ZeroDivisionError):
            change = None
        return {
            "contract": self._contract(symbol),
            "last": last,
            "highest_bid": raw.get("bidPr"),
            "lowest_ask": raw.get("askPr"),
            "mark_price": raw.get("markPrice"),
            "index_price": raw.get("indexPrice"),
            "funding_rate": raw.get("fundingRate"),
            "volume_24h_quote": raw.get("usdtVolume") or raw.get("quoteVolume"),
            "total_size": raw.get("holdingAmount"),
            "change_percentage": change,
            "timestamp": raw.get("ts"),
            "raw": dict(raw),
        }

    async def get_tickers(self) -> list[dict[str, Any]]:
        payload = await self.request("GET", "/mix/market/tickers", params={"productType": self.product_type})
        return [self._ticker(item) for item in payload if isinstance(item, Mapping)] if isinstance(payload, list) else []

    async def get_ticker(self, contract: str) -> dict[str, Any]:
        payload = await self.request(
            "GET", "/mix/market/ticker", params={"productType": self.product_type, "symbol": self._symbol(contract)}
        )
        if isinstance(payload, list) and payload:
            return self._ticker(payload[0])
        return self._ticker(payload) if isinstance(payload, Mapping) else {}

    @staticmethod
    def _granularity(interval: str) -> str:
        return {"5m": "5m", "15m": "15m", "30m": "30m", "4h": "4H"}.get(interval, interval)

    async def get_candlesticks(
        self,
        contract: str,
        interval: str,
        limit: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
        contract_prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "symbol": self._symbol(contract),
            "productType": self.product_type,
            "granularity": self._granularity(interval),
            "limit": min(int(limit or 100), 1000),
        }
        if from_ts is not None:
            params["startTime"] = int(from_ts * 1000)
        if to_ts is not None:
            params["endTime"] = int(to_ts * 1000)
        payload = await self.request("GET", "/mix/market/candles", params=params)
        result: list[dict[str, Any]] = []
        for row in payload if isinstance(payload, list) else []:
            if not isinstance(row, list) or len(row) < 6:
                continue
            result.append({"t": float(row[0]) / 1000, "o": row[1], "h": row[2], "l": row[3], "c": row[4], "v": row[5]})
        return result

    async def get_trades(self, contract: str, limit: int = 1000) -> list[dict[str, Any]]:
        payload = await self.request(
            "GET",
            "/mix/market/fills",
            params={"symbol": self._symbol(contract), "productType": self.product_type, "limit": min(limit, 100)},
        )
        result = []
        for row in payload if isinstance(payload, list) else []:
            if not isinstance(row, Mapping):
                continue
            size = _positive(row.get("size"))
            side = str(row.get("side", "")).lower()
            result.append({
                "id": row.get("tradeId"),
                "price": row.get("price"),
                "size": size if side == "buy" else -size,
                "create_time_ms": row.get("tradeTime") or row.get("ts"),
            })
        return result

    async def get_contract_stats(
        self, contract: str, from_ts: int | None = None, limit: int | None = None, interval: str = "30m"
    ) -> list[dict[str, Any]]:
        try:
            payload = await self.request(
                "GET",
                "/mix/market/open-interest",
                params={"symbol": self._symbol(contract), "productType": self.product_type},
            )
            rows = payload.get("openInterestList", payload) if isinstance(payload, Mapping) else payload
            return [{"open_interest": row.get("openInterest") if isinstance(row, Mapping) else row} for row in rows or []]
        except Exception:
            ticker = await self.get_ticker(contract)
            return [{"open_interest": ticker.get("total_size")}] if ticker else []

    async def get_funding_rates(
        self, contract: str, from_ts: int | None = None, to_ts: int | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        payload = await self.request(
            "GET",
            "/mix/market/current-fund-rate",
            params={"symbol": self._symbol(contract), "productType": self.product_type},
        )
        rows = payload if isinstance(payload, list) else [payload] if isinstance(payload, Mapping) else []
        return [{"r": row.get("fundingRate"), "t": row.get("fundingTime")} for row in rows]

    @staticmethod
    def _position(raw: Mapping[str, Any]) -> dict[str, Any]:
        total = _positive(raw.get("total"))
        side = str(raw.get("holdSide", "")).lower()
        signed = total if side in {"long", "buy"} else -total
        mode = str(raw.get("posMode", "one_way_mode")).lower()
        margin_mode = str(raw.get("marginMode", "crossed")).lower()
        return {
            "contract": BitgetRestClient._contract(str(raw.get("symbol", ""))),
            "size": signed,
            "entry_price": raw.get("openPriceAvg"),
            "mark_price": raw.get("markPrice"),
            "unrealised_pnl": raw.get("unrealizedPL"),
            "realised_pnl": raw.get("achievedProfits"),
            "margin": raw.get("marginSize"),
            "initial_margin": raw.get("marginSize"),
            "leverage": raw.get("leverage"),
            "liq_price": raw.get("liquidationPrice"),
            "pos_margin_mode": "cross" if margin_mode == "crossed" else "isolated",
            "mode": "single" if mode == "one_way_mode" else "dual",
            "cross_leverage_limit": raw.get("leverage") if margin_mode == "crossed" else None,
            "update_time": raw.get("uTime") or raw.get("cTime"),
            "raw": dict(raw),
        }

    async def get_positions(self, contract: str | None = None) -> list[dict[str, Any]]:
        payload = await self.request(
            "GET",
            "/mix/position/all-position",
            params={"productType": self.product_type, "marginCoin": self.margin_coin},
            authenticated=True,
        )
        result = []
        for row in payload if isinstance(payload, list) else []:
            if not isinstance(row, Mapping) or _positive(row.get("total")) <= 0:
                continue
            normalized = self._position(row)
            if contract is None or normalized["contract"].upper() == contract.upper():
                result.append(normalized)
        return result

    async def get_position(self, contract: str) -> dict[str, Any] | None:
        positions = await self.get_positions(contract)
        return positions[0] if positions else None

    async def get_account(self) -> dict[str, Any]:
        payload = await self.request(
            "GET", "/mix/account/accounts", params={"productType": self.product_type}, authenticated=True
        )
        rows = payload if isinstance(payload, list) else []
        row = next((item for item in rows if str(item.get("marginCoin", "")).upper() == self.margin_coin), rows[0] if rows else {})
        if not isinstance(row, Mapping):
            return {}
        pos_mode = str(row.get("posMode", "")).strip().lower()
        margin_mode = str(row.get("marginMode", "")).strip().lower()
        return {
            "total": row.get("accountEquity") or row.get("usdtEquity"),
            "available": row.get("available"),
            "unrealised_pnl": row.get("unrealizedPL") or row.get("crossedUnrealizedPL"),
            "position_initial_margin": row.get("crossedMargin") or row.get("isolatedMargin"),
            "maintenance_margin": row.get("unionMm"),
            "order_margin": row.get("locked"),
            "currency": self.margin_coin,
            # The official account-list response includes both posMode and
            # marginMode.  Preserve them so the order layer can verify the
            # account state instead of repeatedly guessing and switching the
            # global position mode before every first order.
            "in_dual_mode": (pos_mode == "hedge_mode") if pos_mode else None,
            "position_mode": "dual" if pos_mode == "hedge_mode" else "single" if pos_mode == "one_way_mode" else None,
            "pos_margin_mode": "cross" if margin_mode == "crossed" else "isolated" if margin_mode == "isolated" else None,
            "cross_leverage_limit": row.get("crossedMarginLeverage"),
            "raw": dict(row),
        }

    async def get_open_orders(self, contract: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"productType": self.product_type, "limit": min(limit, 100)}
        if contract:
            params["symbol"] = self._symbol(contract)
        payload = await self.request("GET", "/mix/order/orders-pending", params=params, authenticated=True)
        rows = payload.get("entrustedList", []) if isinstance(payload, Mapping) else payload
        result = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, Mapping):
                continue
            size = _positive(row.get("size"))
            signed = size if str(row.get("side", "")).lower() == "buy" else -size
            pos_mode = str(row.get("posMode", "")).lower()
            result.append({
                "id": row.get("orderId"),
                "id_string": row.get("orderId"),
                "clientOid": row.get("clientOid"),
                "text": row.get("clientOid"),
                "contract": self._contract(str(row.get("symbol", ""))),
                "size": signed,
                "price": row.get("price"),
                "state": row.get("status"),
                "mode": "single" if pos_mode == "one_way_mode" else "dual" if pos_mode == "hedge_mode" else "",
                "margin_mode": "cross" if str(row.get("marginMode", "crossed")).lower() == "crossed" else "isolated",
                "create_time": _positive(row.get("cTime")) / 1000,
                "raw": dict(row),
            })
        return result

    async def get_risk_limit_tiers(self, contract: str) -> list[dict[str, Any]]:
        return []

    async def set_position_mode(self, position_mode: str = "single") -> dict[str, Any]:
        mode = "one_way_mode" if str(position_mode).lower() in {"single", "one_way_mode"} else "hedge_mode"
        payload = await self.request(
            "POST",
            "/mix/account/set-position-mode",
            authenticated=True,
            json_body={"productType": self.product_type, "posMode": mode},
        )
        actual = str(payload.get("posMode", "") if isinstance(payload, Mapping) else "").lower()
        if actual and actual != mode:
            raise GateAPIError(f"Bitget returned position mode {actual}, expected {mode}", endpoint="/api/v2/mix/account/set-position-mode")
        return {"position_mode": "single" if mode == "one_way_mode" else "dual", "in_dual_mode": mode == "hedge_mode"}

    async def set_position_margin_mode(self, contract: str, margin_mode: str = "cross") -> dict[str, Any]:
        mode = "crossed" if str(margin_mode).lower() in {"cross", "crossed"} else "isolated"
        payload = await self.request(
            "POST",
            "/mix/account/set-margin-mode",
            authenticated=True,
            json_body={
                "symbol": self._symbol(contract),
                "productType": self.product_type,
                "marginCoin": self.margin_coin,
                "marginMode": mode,
            },
        )
        actual = str(payload.get("marginMode", "") if isinstance(payload, Mapping) else "").lower()
        if actual and actual != mode:
            raise GateAPIError(f"Bitget returned margin mode {actual}, expected {mode}", endpoint="/api/v2/mix/account/set-margin-mode")
        return {"contract": contract, "pos_margin_mode": "cross" if mode == "crossed" else "isolated", "marginMode": actual or mode}

    async def set_leverage(self, contract: str, leverage: float, margin_mode: str = "cross", dual_side: str | None = None) -> dict[str, Any]:
        mode = "crossed" if str(margin_mode).lower() in {"cross", "crossed"} else "isolated"
        body: dict[str, Any] = {
            "symbol": self._symbol(contract),
            "productType": self.product_type,
            "marginCoin": self.margin_coin,
            "leverage": _text(leverage),
        }
        if mode == "isolated" and dual_side:
            body["holdSide"] = dual_side
        payload = await self.request("POST", "/mix/account/set-leverage", authenticated=True, json_body=body)
        result = payload if isinstance(payload, Mapping) else {}
        actual_mode = str(result.get("marginMode", "")).lower()
        returned_margin_mode = (
            "cross" if actual_mode == "crossed" else "isolated" if actual_mode == "isolated" else None
        )
        return {
            "contract": contract,
            "pos_margin_mode": returned_margin_mode,
            "marginMode": actual_mode or None,
            "leverage": result.get("crossMarginLeverage") or result.get("leverage") or _text(leverage),
            "cross_leverage_limit": result.get("crossMarginLeverage") or _text(leverage),
        }

    async def set_cross_leverage_legacy(self, contract: str, leverage: float) -> dict[str, Any]:
        return await self.set_leverage(contract, leverage, "cross")

    async def place_futures_order(self, body: dict[str, Any]) -> dict[str, Any]:
        contract = str(body["contract"])
        signed = float(body.get("size") or 0)
        reduce_only = bool(body.get("reduce_only"))
        if reduce_only and signed == 0:
            position = await self.get_position(contract)
            if not position:
                raise GateAPIError(f"no position to close for {contract}", endpoint="/mix/order/place-order")
            signed = -abs(float(position["size"])) if float(position["size"]) > 0 else abs(float(position["size"]))
        side = "buy" if signed > 0 else "sell"
        order_type = "limit" if str(body.get("price", "0")) not in {"", "0", "0.0"} else "market"
        request_body: dict[str, Any] = {
            "symbol": self._symbol(contract),
            "productType": self.product_type,
            "marginMode": "crossed",
            "marginCoin": self.margin_coin,
            "size": _text(abs(signed)),
            "side": side,
            "orderType": order_type,
            "reduceOnly": "YES" if reduce_only else "NO",
            "clientOid": body.get("text") or body.get("clientOid") or f"bot-{int(time.time() * 1000)}",
        }
        if order_type == "limit":
            request_body["price"] = _text(body["price"])
            request_body["force"] = "post_only" if str(body.get("tif", "gtc")).lower() == "post_only" else str(body.get("tif", "gtc")).lower()
        stop = body.get("tpsl_sl_trigger_price")
        if stop not in (None, "", "0", 0):
            request_body["presetStopLossPrice"] = _text(stop)
        take_profit = body.get("tpsl_tp_trigger_price")
        if take_profit not in (None, "", "0", 0):
            request_body["presetStopSurplusPrice"] = _text(take_profit)
        # Do not send an execute price of zero.  Bitget treats the optional
        # execute-price field as a limit execution price and rejects `0` with
        # 40808 (exeTakeProfitPrice/SL must be greater than zero).  Omitting
        # it keeps the exchange-side preset protection as a market-triggered
        # protection layer; the independent plan orders are installed after
        # the limit entry fills.
        payload = await self.request("POST", "/mix/order/place-order", authenticated=True, json_body=request_body)
        result = payload if isinstance(payload, Mapping) else {}
        order_id = result.get("orderId")
        return {"id": order_id, "id_string": order_id, "clientOid": result.get("clientOid"), "status": "open", "raw": result}

    async def cancel_futures_order(self, order_id: str | int) -> dict[str, Any]:
        order = await self._find_order(str(order_id))
        if not order:
            return {"orderId": str(order_id), "status": "not_found"}
        payload = await self.request(
            "POST",
            "/mix/order/cancel-order",
            authenticated=True,
            json_body={
                "orderId": str(order_id),
                "symbol": self._symbol(str(order["contract"])),
                "productType": self.product_type,
                "marginCoin": self.margin_coin,
            },
        )
        return dict(payload) if isinstance(payload, Mapping) else {"raw": payload}

    async def _find_order(self, order_id: str) -> dict[str, Any] | None:
        orders = await self.get_open_orders()
        return next((item for item in orders if str(item.get("id")) == order_id), None)

    async def create_price_order(self, body: dict[str, Any]) -> dict[str, Any]:
        initial = body.get("initial", {})
        trigger = body.get("trigger", {})
        contract = str(initial.get("contract", ""))
        order_type = str(body.get("order_type", ""))
        is_long = "long" in order_type
        is_stop = order_type.startswith("close-")
        position = await self.get_position(contract)
        size = abs(float(position.get("size", 0))) if is_stop and position else abs(float(initial.get("size") or initial.get("amount") or 0))
        contract_info = self._contracts.get(contract.upper())
        if not contract_info:
            await self.get_contracts()
            contract_info = self._contracts.get(contract.upper())
        if contract_info:
            step = Decimal(str(contract_info.get("raw", {}).get("sizeMultiplier") or "0"))
            if step > 0:
                size = float((Decimal(str(size)) / step).to_integral_value(rounding=ROUND_DOWN) * step)
        if size <= 0:
            raise GateAPIError(f"protection size is zero for {contract}", endpoint="/mix/order/place-tpsl-order")
        plan_type = "loss_plan" if is_stop else "profit_plan"
        # Bitget's classic plan endpoint expects buy/sell in one-way mode and
        # long/short only in hedge mode.  The application forces one-way mode.
        hold_side = "buy" if is_long else "sell"
        payload = await self.request(
            "POST",
            "/mix/order/place-tpsl-order",
            authenticated=True,
            json_body={
                "symbol": self._symbol(contract),
                "productType": self.product_type,
                "marginCoin": self.margin_coin,
                "planType": plan_type,
                "triggerPrice": _text(trigger["price"]),
                "triggerType": "mark_price" if int(trigger.get("price_type", 0)) == 1 else "fill_price",
                "executePrice": "0",
                "holdSide": hold_side,
                "size": _text(size),
                "clientOid": initial.get("text") or f"bot-tpsl-{int(time.time() * 1000)}",
            },
        )
        result = payload if isinstance(payload, Mapping) else {}
        order_id = result.get("orderId") or result.get("triggerId")
        return {"id": order_id, "id_string": order_id, "clientOid": result.get("clientOid"), "raw": result}

    async def get_price_orders(
        self,
        status: str = "open",
        contract: str | None = None,
        limit: int = 100,
        order_id: str | None = None,
        client_oid: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"productType": self.product_type, "planType": "profit_loss", "limit": min(limit, 100)}
        if contract:
            params["symbol"] = self._symbol(contract)
        if order_id:
            params["orderId"] = str(order_id)
        elif client_oid:
            params["clientOid"] = str(client_oid)
        payload = await self.request("GET", "/mix/order/orders-plan-pending", params=params, authenticated=True)
        rows = payload.get("entrustedList", []) if isinstance(payload, Mapping) else []
        result = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            symbol = self._contract(str(row.get("symbol", "")))
            if contract and symbol.upper() != contract.upper():
                continue
            plan_type = str(row.get("planType", ""))
            side = str(row.get("posSide", "")).lower()
            if side == "net":
                side = "long" if str(row.get("side", "")).lower() == "buy" else "short"
            order_type = ("close-long-position" if side == "long" else "close-short-position") if plan_type == "loss_plan" else ("plan-close-long-position" if side == "long" else "plan-close-short-position")
            result.append({
                "id": row.get("orderId"),
                "id_string": row.get("orderId"),
                "contract": symbol,
                "order_type": order_type,
                "plan_type": plan_type,
                "status": row.get("planStatus"),
                "initial": {"contract": symbol, "size": row.get("size")},
                "trigger": {"price": row.get("triggerPrice"), "price_type": 1 if row.get("triggerType") == "mark_price" else 0},
                "text": row.get("clientOid"),
                "clientOid": row.get("clientOid"),
                "margin_mode": "cross" if str(row.get("marginMode", "crossed")).lower() == "crossed" else "isolated",
                "mode": "single" if str(row.get("posMode", "one_way_mode")).lower() == "one_way_mode" else "dual",
                "raw": dict(row),
            })
        return result

    async def get_order_detail(
        self,
        order_id: str | None = None,
        client_oid: str | None = None,
        contract: str | None = None,
    ) -> dict[str, Any] | None:
        """Read one ordinary order after submission.

        A successful place-order response is not enough to prove that a limit
        order is still live: Bitget can fill/cancel it between the response
        and the pending-order query.  The detail endpoint lets the service
        distinguish that race from an order which was never accepted.
        """
        if not order_id and not client_oid:
            return None
        params: dict[str, Any] = {"productType": self.product_type}
        if order_id:
            params["orderId"] = str(order_id)
        else:
            params["clientOid"] = str(client_oid)
        if contract:
            params["symbol"] = self._symbol(contract)
        payload = await self.request("GET", "/mix/order/detail", params=params, authenticated=True)
        if not isinstance(payload, Mapping) or not payload:
            return None
        symbol = self._contract(str(payload.get("symbol", contract or "")))
        size = _positive(payload.get("size"))
        signed = size if str(payload.get("side", "")).lower() == "buy" else -size
        pos_mode = str(payload.get("posMode", "")).lower()
        return {
            "id": payload.get("orderId") or order_id,
            "id_string": payload.get("orderId") or order_id,
            "clientOid": payload.get("clientOid") or client_oid,
            "text": payload.get("clientOid") or client_oid,
            "contract": symbol,
            "size": signed,
            "price": payload.get("price"),
            "state": payload.get("state") or payload.get("status"),
            "mode": "single" if pos_mode == "one_way_mode" else "dual" if pos_mode == "hedge_mode" else "",
            "margin_mode": "cross" if str(payload.get("marginMode", "crossed")).lower() == "crossed" else "isolated",
            "create_time": _positive(payload.get("cTime")) / 1000,
            "raw": dict(payload),
        }

    async def amend_price_order(self, body: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("Bitget trigger orders are replaced by cancel/create in the manager")

    async def cancel_price_order(self, order_id: str | int) -> dict[str, Any]:
        order = next(
            (item for item in await self.get_price_orders(order_id=str(order_id)) if str(item.get("id")) == str(order_id)),
            None,
        )
        if not order:
            return {"orderId": str(order_id), "status": "not_found"}
        plan_type = str(order.get("plan_type") or order.get("raw", {}).get("planType") or "normal_plan")
        payload = await self.request(
            "POST",
            "/mix/order/cancel-plan-order",
            authenticated=True,
            json_body={
                "orderIdList": [{"orderId": str(order_id)}],
                "symbol": self._symbol(str(order["contract"])),
                "productType": self.product_type,
                "marginCoin": self.margin_coin,
                "planType": plan_type,
            },
        )
        return dict(payload) if isinstance(payload, Mapping) else {"raw": payload}

    async def cancel_all_price_orders(self, contract: str | None = None) -> Any:
        orders = await self.get_price_orders(contract=contract)
        if not orders:
            return {"successList": []}
        success: list[dict[str, Any]] = []
        failure: list[dict[str, Any]] = []
        for item in orders:
            try:
                response = await self.cancel_price_order(item["id"])
                success.append({"orderId": str(item["id"]), **response})
            except Exception as exc:
                failure.append({"orderId": str(item["id"]), "errorMsg": str(exc)})
        return {"successList": success, "failureList": failure}
