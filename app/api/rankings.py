from fastapi import APIRouter, Query, Request

from app.dependencies import state_from_request

router = APIRouter(prefix="/api", tags=["rankings"])


async def _rankings(request: Request, key: str | None = None):
    result = await state_from_request(request).repository.latest_scan()
    if not result:
        return {"scan_id": None, "generated_at": None, "combined": [], "long": [], "short": [], "diagnostics": {"status": "no_scan_yet"}}
    if key:
        return {"scan_id": result.get("scan_id"), "generated_at": result.get("finished_at"), "ranking_type": key, "items": result.get("rankings", {}).get(key, [])}
    return {"scan_id": result.get("scan_id"), "generated_at": result.get("finished_at"), **result.get("rankings", {}), "diagnostics": result.get("diagnostics", {})}


@router.get("/rankings")
async def rankings(request: Request, top_n: int = Query(10, ge=1, le=10)):
    return await _rankings(request)


@router.get("/rankings/long")
async def long_rankings(request: Request):
    return await _rankings(request, "long")


@router.get("/rankings/short")
async def short_rankings(request: Request):
    return await _rankings(request, "short")


@router.get("/rankings/history")
async def rankings_history(request: Request, limit: int = Query(50, ge=1, le=200)):
    return {"items": await state_from_request(request).repository.scan_history(limit)}

