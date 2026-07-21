from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class APIError(BaseModel):
    code: str
    message: str
    request_id: str
    details: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str
    service: str
    timestamp: datetime
    database: str
    gate_api: str
    discord: str
    scheduler: str
    trading: str = "disabled"


class PageInfo(BaseModel):
    page: int = 1
    page_size: int = 50
    total: int = 0
