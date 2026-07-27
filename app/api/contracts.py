from fastapi import APIRouter, HTTPException, Request

from app.dependencies import state_from_request

router = APIRouter(prefix="/api/contracts", tags=["contracts"])


@router.get("")
async def contracts(request: Request):
    state = state_from_request(request)
    try:
        return {"items": await state.gate.rest.get_contracts()}
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"code": "gate_unavailable", "message": str(exc)}) from exc


@router.get("/{contract}")
async def contract(request: Request, contract: str):
    state = state_from_request(request)
    contracts = await state.gate.rest.get_contracts(include_delisted=True)
    for item in contracts:
        if item.get("name", "").upper() == contract.upper():
            return item
    raise HTTPException(status_code=404, detail="contract not found")


@router.get("/{contract}/history")
async def contract_history(request: Request, contract: str, interval: str = "30m", limit: int = 240):
    state = state_from_request(request)
    return {"contract": contract, "interval": interval, "candles": await state.gate.rest.get_candlesticks(contract.upper(), interval, limit=min(limit, 2000))}

