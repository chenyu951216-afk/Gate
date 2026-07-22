from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Candle(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume_contracts: float | None = None
    turnover_usdt: float | None = None
    is_closed: bool = True


class MarketSnapshot(BaseModel):
    contract: str
    timestamp: datetime
    last: float | None = None
    bid: float | None = None
    ask: float | None = None
    spread_pct: float | None = None
    turnover_24h_usdt: float | None = None
    mark_price: float | None = None
    index_price: float | None = None
    funding_rate: float | None = None
    open_interest: float | None = None
    data_available: dict[str, bool] = Field(default_factory=dict)
    missing_data: list[str] = Field(default_factory=list)


class ContractInfo(BaseModel):
    name: str
    status: str
    type: str | None = None
    quanto_multiplier: float | None = None
    contract_size: float | None = None
    leverage_min: float | None = None
    leverage_max: float | None = None
    order_price_round: float | None = None
    mark_price_round: float | None = None
    order_size_min: float | None = None
    order_size_max: float | None = None
    enable_decimal: bool = False
    mark_price: float | None = None
    index_price: float | None = None
    launch_time: datetime | None = None
    delisting_time: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
