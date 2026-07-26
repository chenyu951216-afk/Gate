from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class BacktestRequest(BaseModel):
    replay_job_id: str
    ranking_types: list[str] = Field(default_factory=lambda: ["combined"])
    top_n: int = Field(default=10, ge=1, le=10)
    holding_bars: int = Field(default=4, ge=1, le=1000)
    stop_atr: float | None = Field(default=2.0, gt=0)
    take_atr: float | None = Field(default=3.0, gt=0)
    fee_pct: float = Field(default=0.05, ge=0)
    slippage_pct: float = Field(default=0.02, ge=0)


class BacktestResult(BaseModel):
    run_id: str
    created_at: datetime
    parameters: dict[str, Any]
    metrics: dict[str, float | int | None]
    trades: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)

