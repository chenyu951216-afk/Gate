from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from app.dependencies import require_bearer, state_from_request
from app.replay.reports import export_csv, export_html, export_json

router = APIRouter(prefix="/api/replay", tags=["replay"])
templates = Jinja2Templates(directory="app/templates")


@router.post("")
async def create_replay(request: Request):
    await require_bearer(request)
    body = await request.json()
    state = state_from_request(request)
    job = await state.replay.create_job(body)
    return {"job_id": job["job_id"], "status": job["status"], "total": job["total"]}


@router.get("/{job_id}/status")
async def replay_status(request: Request, job_id: str):
    job = await state_from_request(request).replay.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="replay job not found")
    return {key: job.get(key) for key in ("job_id", "status", "phase", "current_timepoint", "created_at", "completed", "total", "diagnostics")}


@router.get("/{job_id}/results")
async def replay_results(request: Request, job_id: str):
    job = await state_from_request(request).replay.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="replay job not found")
    return {"job_id": job_id, "status": job["status"], "results": job.get("results", [])}


@router.get("/{job_id}/results/{timestamp}")
async def replay_result(request: Request, job_id: str, timestamp: str):
    job = await state_from_request(request).replay.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="replay job not found")
    for item in job.get("results", []):
        if item.get("aligned_time", "").startswith(timestamp):
            return item
    raise HTTPException(status_code=404, detail="timepoint not found")


@router.get("/{job_id}/diagnostics")
async def replay_diagnostics(request: Request, job_id: str):
    job = await state_from_request(request).replay.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="replay job not found")
    return {"job_id": job_id, "items": [item.get("diagnostics", {}) for item in job.get("results", [])]}


@router.get("/{job_id}/export.json")
async def replay_export_json(request: Request, job_id: str):
    job = await state_from_request(request).replay.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="replay job not found")
    return Response(export_json(job), media_type="application/json", headers={"Content-Disposition": f"attachment; filename={job_id}.json"})


@router.get("/{job_id}/export.csv")
async def replay_export_csv(request: Request, job_id: str):
    job = await state_from_request(request).replay.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="replay job not found")
    return Response(export_csv(job), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={job_id}.csv"})


@router.get("/{job_id}/export.html")
async def replay_export_html(request: Request, job_id: str):
    job = await state_from_request(request).replay.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="replay job not found")
    return Response(export_html(job), media_type="text/html")


@router.delete("/{job_id}")
async def delete_replay(request: Request, job_id: str):
    await require_bearer(request)
    state = state_from_request(request)
    state.replay.cancel_events.pop(job_id, None)
    job = await state.replay.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="replay job not found")
    job["status"] = "deleted"
    await state.repository.save_replay(job)
    return {"status": "deleted", "job_id": job_id}


@router.post("/{job_id}/cancel")
async def cancel_replay(request: Request, job_id: str):
    await require_bearer(request)
    if not await state_from_request(request).replay.cancel(job_id):
        raise HTTPException(status_code=404, detail="replay job not found")
    return {"status": "cancelling", "job_id": job_id}

