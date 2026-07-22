from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ScanRequest(BaseModel):
    dry_run: bool = False
    notify_discord: bool | None = None
    top_n: int = Field(default=10, ge=1, le=10)


class ScanRunResponse(BaseModel):
    scan_id: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    elapsed_seconds: float | None = None
    universe_total: int = 0
    excluded_count: int = 0
    successful_count: int = 0
    error_count: int = 0
    dry_run: bool = False
    rankings: dict[str, Any] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)

