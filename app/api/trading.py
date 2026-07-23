import asyncio
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.dependencies import require_bearer, state_from_request

router = APIRouter(prefix="/api/trading", tags=["trading"])


class PauseRequest(BaseModel):
    reason: str = Field(default="manual pause", max_length=255)


class ModeRequest(BaseModel):
    mode: str = Field(pattern="^(live|test|formal|production|real)$")


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _position_view(position: dict[str, Any]) -> dict[str, Any]:
    size = _number(position.get("size"), 0.0) or 0.0
    entry = _number(position.get("entry_price"))
    mark = _number(position.get("mark_price"))
    unrealised = _number(position.get("unrealised_pnl"))
    margin = _number(position.get("margin")) or _number(position.get("initial_margin"))
    raw_mode = str(position.get("pos_margin_mode") or position.get("margin_mode") or "").lower()
    if raw_mode not in {"cross", "isolated"} and _number(position.get("leverage"), -1.0) == 0:
        raw_mode = "cross"
    return {
        "contract": position.get("contract"),
        "side": "LONG" if size > 0 else "SHORT" if size < 0 else "FLAT",
        "size": abs(size),
        "raw_size": size,
        "entry_price": entry,
        "mark_price": mark,
        "leverage": _number(position.get("leverage")) or _number(position.get("cross_leverage_limit")),
        "margin_mode": raw_mode or None,
        "position_mode": position.get("mode"),
        "cross_leverage_limit": _number(position.get("cross_leverage_limit")),
        "unrealised_pnl": unrealised,
        "realised_pnl": _number(position.get("realised_pnl")),
        "liquidation_price": _number(position.get("liq_price")) or _number(position.get("liquidation_price")),
        "margin": margin,
        "maintenance_margin": _number(position.get("maintenance_margin")),
        "pnl_percent": unrealised / margin * 100 if unrealised is not None and margin else None,
        "entry_time": position.get("update_time") or position.get("open_time"),
    }


def _account_view(account: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "total",
        "available",
        "unrealised_pnl",
        "position_initial_margin",
        "maintenance_margin",
        "order_margin",
        "cross_order_margin",
        "cross_initial_margin",
        "cross_maintenance_margin",
        "currency",
        "in_dual_mode",
    )
    result = {field: account.get(field) for field in fields if field in account}
    result["total_balance"] = _number(account.get("total"))
    result["available_balance"] = _number(account.get("available"))
    result["unrealised_pnl"] = _number(account.get("unrealised_pnl"))
    result["position_initial_margin"] = _number(account.get("position_initial_margin"))
    result["maintenance_margin"] = _number(account.get("maintenance_margin"))
    result["order_margin"] = _number(account.get("order_margin"))
    return result


@router.get("/status")
async def trading_status(request: Request) -> dict[str, Any]:
    return await state_from_request(request).trading.status()


@router.get("/positions")
async def trading_positions(request: Request) -> dict[str, Any]:
    await require_bearer(request)
    state = state_from_request(request)
    positions: list[dict[str, Any]] = []
    bitget_status = "not_configured"
    if state.settings.bitget_api_key and state.settings.bitget_api_secret and state.settings.bitget_api_passphrase:
        positions = await state.bitget.rest.get_positions()
        bitget_status = "connected"
    managed = await state.repository.list_managed_positions(active_only=False)
    return {
        "positions": [_position_view(item) for item in positions],
        "managed": managed,
        "exchange": "bitget",
        "bitget_status": bitget_status,
    }


@router.get("/overview")
async def trading_overview(request: Request) -> dict[str, Any]:
    await require_bearer(request)
    state = state_from_request(request)
    if not (state.settings.bitget_api_key and state.settings.bitget_api_secret and state.settings.bitget_api_passphrase):
        return {"exchange": "bitget", "bitget_status": "not_configured", "account": {}, "positions": [], "open_orders": [], "protection_orders": [], "managed": []}
    results_raw: tuple[Any, Any, Any, Any] = await asyncio.gather(
        state.bitget.rest.get_account(),
        state.bitget.rest.get_positions(),
        state.bitget.rest.get_open_orders(limit=100),
        state.bitget.rest.get_price_orders(status="open", limit=100),
        return_exceptions=True,
    )
    account_result, positions_result, open_orders_result, protection_orders_result = results_raw
    results = {
        "account": account_result,
        "positions": positions_result,
        "open_orders": open_orders_result,
        "protection_orders": protection_orders_result,
    }
    errors = {
        name: f"{type(value).__name__}: {value}"
        for name, value in results.items()
        if isinstance(value, Exception)
    }
    account = results["account"] if isinstance(results["account"], dict) else {}
    positions = results["positions"] if isinstance(results["positions"], list) else []
    open_orders = results["open_orders"] if isinstance(results["open_orders"], list) else []
    protection_orders = results["protection_orders"] if isinstance(results["protection_orders"], list) else []
    position_views = [_position_view(item) for item in positions if abs(_number(item.get("size"), 0.0) or 0.0) > 0]
    return {
        "exchange": "bitget",
        "bitget_status": "partial_error" if errors else "connected",
        "bitget_errors": errors,
        "account": _account_view(account),
        "positions": position_views,
        "open_orders": open_orders,
        "protection_orders": protection_orders,
        "managed": await state.repository.list_managed_positions(active_only=True),
        "summary": {
            "position_count": len(position_views),
            "cross_position_count": sum(1 for item in position_views if item.get("margin_mode") == "cross"),
            "isolated_position_count": sum(1 for item in position_views if item.get("margin_mode") == "isolated"),
            "unrealised_pnl": sum(item.get("unrealised_pnl") or 0 for item in position_views),
            "protection_order_count": len(protection_orders),
            "open_order_count": len(open_orders),
        },
    }


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


@router.post("/mode")
async def change_trading_mode(request: Request, body: ModeRequest) -> dict[str, Any]:
    await require_bearer(request)
    state = state_from_request(request)
    result = await state.trading.set_mode(body.mode)
    return {"status": "mode_changed", **result, "exchange": "bitget"}


@router.post("/manage-once")
async def manage_once(request: Request) -> dict[str, Any]:
    await require_bearer(request)
    return await state_from_request(request).trading.manage_once()
