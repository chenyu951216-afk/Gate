from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.dependencies import require_bearer, state_from_request

router = APIRouter(prefix="/api/trading", tags=["trading"])


class PauseRequest(BaseModel):
    reason: str = Field(default="manual pause", max_length=255)


@router.get("/status")
async def trading_status(request: Request) -> dict[str, Any]:
    return await state_from_request(request).trading.status()


@router.get("/positions")
async def trading_positions(request: Request) -> dict[str, Any]:
    await require_bearer(request)
    state = state_from_request(request)
    positions: list[dict[str, Any]] = []
    gate_status = "not_configured"
    if state.settings.gate_api_key and state.settings.gate_api_secret:
        positions = await state.gate.rest.get_positions()
        gate_status = "connected"
    managed = await state.repository.list_managed_positions(active_only=False)
    return {"positions": positions, "managed": managed, "gate_status": gate_status}


@router.post("/pause")
async def pause_trading(request: Request, body: PauseRequest) -> dict[str, Any]:
    await require_bearer(request)
    state = state_from_request(request)
    return {"status": "paused", **await state.trading.pause(body.reason)}


@router.post("/resume")
async def resume_trading(request: Request) -> dict[str, Any]:
    await require_bearer(request)
    state = state_from_request(request)
    return {"status": "resumed", **await state.trading.resume()}


@router.post("/manage-once")
async def manage_once(request: Request) -> dict[str, Any]:
    await require_bearer(request)
    return await state_from_request(request).trading.manage_once()
