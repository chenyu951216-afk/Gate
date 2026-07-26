from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RankingItem(BaseModel):
    rank: int
    contract: str
    direction: str
    ranking_score: float
    bull_score: float
    bear_score: float
    watch_score: float
    confidence: float
    data_completeness_pct: float
    risk_penalty: float
    direction_edge: float
    market_state: str
    signal_state: str
    risk_flags: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime


class RankingResponse(BaseModel):
    scan_id: str | None
    generated_at: datetime
    combined: list[RankingItem] = Field(default_factory=list)
    long: list[RankingItem] = Field(default_factory=list)
    short: list[RankingItem] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)

