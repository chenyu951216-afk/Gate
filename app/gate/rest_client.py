import asyncio
import hashlib
import hmac
import json
import logging
import random
import time
from collections.abc import Mapping
from typing import Any

import httpx

from app.exceptions import GateAPIError
from app.gate.endpoints import (
    CANDLESTICKS,
    CONTRACTS,
    CONTRACTS_ALL,
    CONTRACT_STATS,
    FUNDING_RATE,
    LIQ_ORDERS,
    ACCOUNTS,
    ORDERS,
    POSITION_LEVERAGE,
    POSITION_LEVERAGE_LEGACY,
    POSITION_CROSS_MODE,
    POSITIONS,
    PRICE_ORDER_AMEND,
    PRICE_ORDERS,
    RISK_LIMIT_TIERS,
    TICKERS,
    TRADES,
)
from app.gate.rate_limiter import AsyncRateLimiter
from app.gate.validators import ensure_list

logger = logging.getLogger(__name__)


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


class GateRestClient:
    def __init__(self, settings: Any, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.gate_rest_base_url.rstrip("/"),
            timeout=settings.gate_request_timeout_seconds,
            transport=transport,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        self.limiter = AsyncRateLimiter(settings.gate_requests_per_second)
        self.semaphore = asyncio.Semaphore(settings.gate_max_concurrency)
        self.breaker = CircuitBreaker(settings.gate_circuit_failure_threshold, settings.gate_circuit_recovery_seconds)

    async def close(self) -> None:
        await self.client.aclose()

    def _auth_headers(self, method: str, path: str, query: str, body: str = "") -> dict[str, str]:
        if not self.settings.gate_api_key or not self.settings.gate_api_secret:
            return {}
        timestamp = str(int(time.time()))
        body_hash = hashlib.sha512(body.encode()).hexdigest()
        signing = "\n".join((method.upper(), "/api/v4" + path, query, body_hash, timestamp))
        signature = hmac.new(
            self.settings.gate_api_secret.encode(), signing.encode(), hashlib.sha512
        ).hexdigest()
        return {"KEY": self.settings.gate_api_key, "Timestamp": timestamp, "SIGN": signature}

    async def request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        authenticated: bool = False,
        json_body: Any | None = None,
    ) -> Any:
        if not self.breaker.allow():
            raise GateAPIError("Gate circuit breaker is open", endpoint=path)
        query = str(httpx.QueryParams(params or {}))
        body = "" if json_body is None else json.dumps(json_body, separators=(",", ":"), ensure_ascii=False)
        headers = self._auth_headers(method, path, query, body) if authenticated else {}
        headers["X-Gate-Size-Decimal"] = "1"
        attempts = max(1, self.settings.gate_retry_attempts)
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
                        delay = retry_after or min(8.0, 0.5 * 2**attempt) + random.uniform(0, 0.2)
                        await asyncio.sleep(delay)
                        continue
                    response.raise_for_status()
                    self.breaker.success()
                    try:
                        return response.json()
                    except json.JSONDecodeError as exc:
                        raise GateAPIError("Gate returned invalid JSON", endpoint=path) from exc
                except httpx.HTTPStatusError as exc:
                    self.breaker.failure()
                    detail = exc.response.text[:300]
                    raise GateAPIError(
                        f"Gate HTTP error {exc.response.status_code}: {detail}",
                        status_code=exc.response.status_code,
                        endpoint=path,
                    ) from exc
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    self.breaker.failure()
                    if attempt + 1 == attempts:
                        raise GateAPIError(f"Gate network failure: {type(exc).__name__}", endpoint=path) from exc
                    await asyncio.sleep(min(8.0, 0.5 * 2**attempt) + random.uniform(0, 0.2))
        raise GateAPIError("Gate request failed", endpoint=path)

    async def get_contracts(self, include_delisted: bool = False) -> list[dict[str, Any]]:
        payload = await self.request("GET", CONTRACTS_ALL if include_delisted else CONTRACTS)
        return ensure_list(payload, CONTRACTS_ALL if include_delisted else CONTRACTS)

    async def get_tickers(self) -> list[dict[str, Any]]:
        return ensure_list(await self.request("GET", TICKERS), TICKERS)

    async def get_candlesticks(
        self,
        contract: str,
        interval: str,
        limit: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
        contract_prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        name = f"{contract_prefix}_{contract}" if contract_prefix else contract
        params: dict[str, Any] = {"contract": name, "interval": interval}
        interval_seconds = {"5m": 300, "15m": 900, "30m": 1800, "4h": 14400}.get(interval)
        if to_ts is not None and from_ts is None and limit is not None and interval_seconds:
            from_ts = to_ts - min(limit, 2000) * interval_seconds
        if from_ts is not None:
            params["from"] = from_ts
        if to_ts is not None:
            params["to"] = to_ts
        if limit is not None and from_ts is None and to_ts is None:
            params["limit"] = min(limit, 2000)
        return ensure_list(await self.request("GET", CANDLESTICKS, params=params), CANDLESTICKS)

    async def get_trades(self, contract: str, limit: int = 1000) -> list[dict[str, Any]]:
        params = {"contract": contract, "limit": min(limit, 1000)}
        return ensure_list(await self.request("GET", TRADES, params=params), TRADES)

    async def get_ticker(self, contract: str) -> dict[str, Any]:
        payload = await self.request("GET", TICKERS, params={"contract": contract})
        items = ensure_list(payload, TICKERS)
        return items[0] if items else {}

    async def get_positions(self, contract: str | None = None) -> list[dict[str, Any]]:
        params = {"contract": contract} if contract else None
        payload = await self.request("GET", POSITIONS, params=params, authenticated=True)
        return ensure_list(payload, POSITIONS)

    async def get_account(self) -> dict[str, Any]:
        payload = await self.request("GET", ACCOUNTS, authenticated=True)
        return dict(payload) if isinstance(payload, Mapping) else {}

    async def get_position(self, contract: str) -> dict[str, Any] | None:
        payload = await self.request("GET", f"{POSITIONS}/{contract}", authenticated=True)
        if isinstance(payload, Mapping):
            return dict(payload)
        items = ensure_list(payload, f"{POSITIONS}/{contract}")
        return items[0] if items else None

    async def get_open_orders(self, contract: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"status": "open", "limit": min(limit, 100)}
        if contract:
            params["contract"] = contract
        payload = await self.request("GET", ORDERS, params=params, authenticated=True)
        return ensure_list(payload, ORDERS)

    async def get_risk_limit_tiers(self, contract: str) -> list[dict[str, Any]]:
        payload = await self.request("GET", RISK_LIMIT_TIERS, params={"contract": contract})
        return ensure_list(payload, RISK_LIMIT_TIERS)

    async def set_leverage(
        self, contract: str, leverage: float, margin_mode: str = "cross", dual_side: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"leverage": str(leverage), "margin_mode": margin_mode}
        if dual_side:
            params["dual_side"] = dual_side
        payload = await self.request(
            "POST", POSITION_LEVERAGE.format(contract=contract), params=params, authenticated=True
        )
        return dict(payload) if isinstance(payload, Mapping) else {"raw": payload}

    async def set_position_margin_mode(self, contract: str, margin_mode: str = "cross") -> dict[str, Any]:
        mode = str(margin_mode).upper()
        if mode not in {"CROSS", "ISOLATED"}:
            raise ValueError("Gate margin mode must be cross or isolated")
        payload = await self.request(
            "POST",
            POSITION_CROSS_MODE,
            authenticated=True,
            json_body={"mode": mode, "contract": contract},
        )
        return dict(payload) if isinstance(payload, Mapping) else {"raw": payload}

    async def set_cross_leverage_legacy(self, contract: str, leverage: float) -> dict[str, Any]:
        """Force cross mode through Gate's legacy leverage endpoint as a fallback."""
        params = {"leverage": "0", "cross_leverage_limit": str(leverage)}
        payload = await self.request(
            "POST",
            POSITION_LEVERAGE_LEGACY.format(contract=contract),
            params=params,
            authenticated=True,
        )
        return dict(payload) if isinstance(payload, Mapping) else {"raw": payload}

    async def place_futures_order(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = await self.request("POST", ORDERS, authenticated=True, json_body=body)
        return dict(payload) if isinstance(payload, Mapping) else {"raw": payload}

    async def cancel_futures_order(self, order_id: str | int) -> dict[str, Any]:
        payload = await self.request("DELETE", f"{ORDERS}/{order_id}", authenticated=True)
        return dict(payload) if isinstance(payload, Mapping) else {"raw": payload}

    async def get_price_orders(
        self, status: str = "open", contract: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"status": status, "limit": min(limit, 100)}
        if contract:
            params["contract"] = contract
        payload = await self.request("GET", PRICE_ORDERS, params=params, authenticated=True)
        return ensure_list(payload, PRICE_ORDERS)

    async def create_price_order(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = await self.request("POST", PRICE_ORDERS, authenticated=True, json_body=body)
        return dict(payload) if isinstance(payload, Mapping) else {"raw": payload}

    async def amend_price_order(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = await self.request("PUT", PRICE_ORDER_AMEND, authenticated=True, json_body=body)
        return dict(payload) if isinstance(payload, Mapping) else {"raw": payload}

    async def cancel_price_order(self, order_id: str | int) -> dict[str, Any]:
        payload = await self.request("DELETE", f"{PRICE_ORDERS}/{order_id}", authenticated=True)
        return dict(payload) if isinstance(payload, Mapping) else {"raw": payload}

    async def cancel_all_price_orders(self, contract: str | None = None) -> Any:
        params = {"contract": contract} if contract else None
        return await self.request("DELETE", PRICE_ORDERS, params=params, authenticated=True)

    async def get_contract_stats(
        self, contract: str, from_ts: int | None = None, limit: int | None = None, interval: str = "30m"
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"contract": contract, "interval": interval}
        if from_ts is not None:
            params["from"] = from_ts
        if limit is not None:
            params["limit"] = min(limit, 2000)
        return ensure_list(await self.request("GET", CONTRACT_STATS, params=params), CONTRACT_STATS)

    async def get_funding_rates(
        self, contract: str, from_ts: int | None = None, to_ts: int | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"contract": contract}
        if from_ts is not None:
            params["from"] = from_ts
        if to_ts is not None:
            params["to"] = to_ts
        if limit is not None:
            params["limit"] = min(limit, 2000)
        return ensure_list(await self.request("GET", FUNDING_RATE, params=params), FUNDING_RATE)

    async def get_liquidation_orders(
        self, contract: str | None = None, from_ts: int | None = None, to_ts: int | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if contract:
            params["contract"] = contract
        if from_ts is not None:
            params["from"] = from_ts
        if to_ts is not None:
            params["to"] = to_ts
        return ensure_list(await self.request("GET", LIQ_ORDERS, params=params, authenticated=True), LIQ_ORDERS)
