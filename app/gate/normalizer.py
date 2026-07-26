from datetime import datetime, timedelta, timezone
from typing import Any

from app.gate.units import turnover_from_candle
from app.gate.validators import ensure_list, number, require_fields
from app.schemas.market import Candle, ContractInfo, MarketSnapshot


def epoch(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def normalize_contract(item: dict[str, Any]) -> ContractInfo:
    require_fields(item, ("name", "status"), "/futures/usdt/contracts")
    return ContractInfo(
        name=str(item["name"]),
        status=str(item["status"]),
        type=item.get("type"),
        quanto_multiplier=number(item.get("quanto_multiplier")),
        contract_size=number(item.get("quanto_multiplier")),
        leverage_min=number(item.get("leverage_min")),
        leverage_max=number(item.get("leverage_max")),
        order_price_round=number(item.get("order_price_round")),
        mark_price_round=number(item.get("mark_price_round")),
        order_size_min=number(item.get("order_size_min")),
        order_size_max=number(item.get("order_size_max")),
        enable_decimal=bool(item.get("enable_decimal", False)),
        mark_price=number(item.get("mark_price")),
        index_price=number(item.get("index_price")),
        launch_time=epoch(item.get("launch_time")),
        delisting_time=epoch(item.get("delisting_time") or item.get("delisted_time")),
        raw=item,
    )


def normalize_candles(payload: Any, quanto_multiplier: float | None = None) -> list[Candle]:
    result: list[Candle] = []
    for item in ensure_list(payload, "/futures/usdt/candlesticks"):
        require_fields(item, ("t", "o", "h", "l", "c"), "/futures/usdt/candlesticks")
        timestamp = epoch(item["t"])
        open_price = number(item["o"], "o", allow_none=False)
        high_price = number(item["h"], "h", allow_none=False)
        low_price = number(item["l"], "l", allow_none=False)
        close = number(item["c"], "c", allow_none=False)
        if None in (timestamp, open_price, high_price, low_price, close):
            continue
        assert timestamp is not None
        assert open_price is not None
        assert high_price is not None
        assert low_price is not None
        assert close is not None
        result.append(
            Candle(
                timestamp=timestamp,
                open=open_price,
                high=high_price,
                low=low_price,
                close=close,
                volume_contracts=number(item.get("v")),
                turnover_usdt=turnover_from_candle(item, close, quanto_multiplier),
                is_closed=bool(item.get("w") is not True),
            )
        )
    return sorted(result, key=lambda item: item.timestamp)


def closed_candles(candles: list[Candle], interval: str, as_of: datetime | None = None) -> list[Candle]:
    seconds = {"5m": 300, "15m": 900, "30m": 1800, "4h": 14400}[interval]
    reference = as_of or datetime.now(timezone.utc)
    return [item for item in candles if item.timestamp + timedelta(seconds=seconds) <= reference]


def normalize_snapshot(
    ticker: dict[str, Any], contract_stats: dict[str, Any] | None = None
) -> MarketSnapshot:
    contract = str(ticker.get("contract", ""))
    bid = number(ticker.get("highest_bid"))
    ask = number(ticker.get("lowest_ask"))
    mid = (bid + ask) / 2 if bid and ask and bid > 0 and ask > 0 else None
    spread = ((ask - bid) / mid * 100) if mid and ask is not None and bid is not None else None
    stats = contract_stats or {}
    available = {
        "last": number(ticker.get("last")) is not None,
        "bid": bid is not None,
        "ask": ask is not None,
        "spread": spread is not None,
        "turnover_24h": number(ticker.get("volume_24h_quote") or ticker.get("volume_24h_usd")) is not None,
        "mark_price": number(ticker.get("mark_price")) is not None,
        "index_price": number(ticker.get("index_price")) is not None,
        "funding_rate": number(ticker.get("funding_rate")) is not None,
        "open_interest": number(stats.get("open_interest") or ticker.get("total_size")) is not None,
    }
    missing = [key for key, value in available.items() if not value]
    return MarketSnapshot(
        contract=contract,
        timestamp=datetime.now(timezone.utc),
        last=number(ticker.get("last")),
        bid=bid,
        ask=ask,
        spread_pct=spread,
        turnover_24h_usdt=number(ticker.get("volume_24h_quote") or ticker.get("volume_24h_usd")),
        mark_price=number(ticker.get("mark_price")),
        index_price=number(ticker.get("index_price")),
        funding_rate=number(ticker.get("funding_rate")),
        open_interest=number(stats.get("open_interest") or ticker.get("total_size")),
        data_available=available,
        missing_data=missing,
    )
