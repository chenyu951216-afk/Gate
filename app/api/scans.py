from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

from app.dependencies import require_bearer, state_from_request
from app.schemas.scan import ScanRequest

router = APIRouter(prefix="/api", tags=["scans"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/status")
async def api_status(request: Request):
    state = state_from_request(request)
    latest = await state.repository.latest_scan()
    scheduler = state.scheduler
    return {
        "service": state.settings.app_name,
        "database_mode": state.repository.mode,
        "discord_enabled": state.notifier.discord.enabled,
        "scheduler_running": bool(scheduler and scheduler.running),
        "scheduler": {
            "running": bool(scheduler and scheduler.running),
            "next_scan_at": scheduler.next_scan_at if scheduler else None,
            "last_scan_started_at": scheduler.last_scan_started_at if scheduler else None,
            "last_scan_finished_at": scheduler.last_scan_finished_at if scheduler else None,
            "last_scan_status": scheduler.last_scan_status if scheduler else None,
            "last_scan_error": scheduler.last_scan_error if scheduler else None,
            "scan_interval": "30m",
            "scan_delay_seconds": state.settings.scan_delay_seconds,
        },
        "trading": await state.trading.status(),
        "latest_scan": latest,
    }


@router.get("/scan/latest")
async def latest_scan(request: Request):
    return await state_from_request(request).repository.latest_scan() or {"status": "no_scan_yet"}


@router.post("/scan")
async def manual_scan(request: Request, body: ScanRequest):
    await require_bearer(request)
    state = state_from_request(request)
    result = await state.scanner.run(body.dry_run, body.notify_discord, body.top_n)
    return result


@router.get("/history", response_class=HTMLResponse, include_in_schema=False)
async def history_page(request: Request):
    state = state_from_request(request)
    return templates.TemplateResponse(request=request, name="history.html", context={"scans": await state.repository.scan_history(100)})
