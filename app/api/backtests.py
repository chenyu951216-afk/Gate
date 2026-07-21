from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.dependencies import require_bearer, state_from_request
from app.schemas.backtest import BacktestRequest

router = APIRouter(prefix="/api/backtest", tags=["backtest"])
templates = Jinja2Templates(directory="app/templates")


@router.post("")
async def run_backtest(request: Request, body: BacktestRequest):
    await require_bearer(request)
    state = state_from_request(request)
    job = await state.replay.get_job(body.replay_job_id)
    if not job:
        raise HTTPException(status_code=404, detail="replay job not found")
    return await state.backtest.run(job, body.model_dump())


@router.get("/page", response_class=HTMLResponse, include_in_schema=False)
async def backtest_page(request: Request):
    return templates.TemplateResponse(request=request, name="backtest.html", context={})

