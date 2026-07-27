from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class NotificationTestRequest(BaseModel):
    message: str = "Gate scanner Discord integration test"


class NotificationDelivery(BaseModel):
    delivery_id: str
    created_at: datetime
    channel: str
    status: str
    message_count: int
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

