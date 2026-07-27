from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.dependencies import state_from_request

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request):
    state = state_from_request(request)
    latest = await state.repository.latest_scan()
    return templates.TemplateResponse(request=request, name="index.html", context={"latest": latest, "settings": state.settings})


@router.get("/status", response_class=HTMLResponse, include_in_schema=False)
async def status_page(request: Request):
    state = state_from_request(request)
    return templates.TemplateResponse(request=request, name="status.html", context={"state": state})


@router.get("/history", response_class=HTMLResponse, include_in_schema=False)
async def history_page(request: Request):
    state = state_from_request(request)
    return templates.TemplateResponse(request=request, name="history.html", context={"scans": await state.repository.scan_history(100)})


@router.get("/contracts/{contract}", response_class=HTMLResponse, include_in_schema=False)
async def contract_page(request: Request, contract: str):
    return templates.TemplateResponse(request=request, name="contract.html", context={"contract": contract})


@router.get("/rankings", response_class=HTMLResponse, include_in_schema=False)
async def rankings_page(request: Request):
    return templates.TemplateResponse(request=request, name="rankings.html", context={"title": "綜合排名", "ranking_type": "combined"})


@router.get("/rankings/long", response_class=HTMLResponse, include_in_schema=False)
async def long_rankings_page(request: Request):
    return templates.TemplateResponse(request=request, name="rankings.html", context={"title": "做多排名", "ranking_type": "long"})


@router.get("/rankings/short", response_class=HTMLResponse, include_in_schema=False)
async def short_rankings_page(request: Request):
    return templates.TemplateResponse(request=request, name="rankings.html", context={"title": "做空排名", "ranking_type": "short"})


@router.get("/replay", response_class=HTMLResponse, include_in_schema=False)
async def replay_page(request: Request):
    return templates.TemplateResponse(request=request, name="replay.html", context={})


@router.get("/replay/{job_id}", response_class=HTMLResponse, include_in_schema=False)
async def replay_result_page(request: Request, job_id: str):
    return templates.TemplateResponse(request=request, name="replay_result.html", context={"job_id": job_id})


@router.get("/backtest", response_class=HTMLResponse, include_in_schema=False)
async def backtest_page(request: Request):
    return templates.TemplateResponse(request=request, name="backtest.html", context={})
