import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from app.replay.diagnostics import new_diagnostics
from app.replay.feature_builder import build_features
from app.replay.historical_collector import HistoricalCollector
from app.replay.snapshot_runner import run_snapshot
from app.replay.timeline import build_timeline

logger = logging.getLogger(__name__)


class ReplayService:
    def __init__(self, gate: Any, repository: Any, settings: Any, notifier: Any | None = None):
        self.gate = gate
        self.repository = repository
        self.settings = settings
        self.notifier = notifier
        self.tasks: dict[str, asyncio.Task] = {}
        self.cancel_events: dict[str, asyncio.Event] = {}
        self.semaphore = asyncio.Semaphore(settings.replay_max_concurrent_jobs)

    def validate_request(self, request: dict[str, Any]) -> list[datetime]:
        points = build_timeline(
            request["start_time"], request["end_time"], request.get("timezone", self.settings.timezone),
            request.get("interval_minutes", 30), request.get("align_mode", "down"), request.get("include_end", True),
        )
        if points and (points[-1] - points[0]).total_seconds() > self.settings.replay_max_hours * 3600:
            raise ValueError("replay range exceeds configured maximum")
        return points

    async def create_job(self, request: dict[str, Any]) -> dict[str, Any]:
        normalized_request = dict(request)
        normalized_request.setdefault("interval_minutes", 30)
        normalized_request.setdefault("align_mode", "down")
        normalized_request.setdefault("include_end", True)
        if not normalized_request.get("end_time"):
            normalized_request["end_time"] = normalized_request["start_time"]
        points = self.validate_request(normalized_request)
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "status": "starting",
            "phase": "queued",
            "created_at": datetime.now(timezone.utc),
            "request": normalized_request,
            "timepoints": [point.isoformat() for point in points],
            "completed": 0,
            "total": len(points),
            "results": [],
            "diagnostics": {"reliable_timepoints": 0, "unreliable_timepoints": 0, "api_errors": 0, "indicator_errors": 0},
        }
        await self.repository.save_replay(job)
        cancel_event = asyncio.Event()
        self.cancel_events[job_id] = cancel_event
        task = asyncio.create_task(self._run(job, points, cancel_event))
        self.tasks[job_id] = task
        return job

    async def _run(self, job: dict[str, Any], points: list[datetime], cancel_event: asyncio.Event) -> None:
        async with self.semaphore:
            try:
                job["status"] = "running"
                job["phase"] = "preparing"
                await self.repository.save_replay(job)
                collector = HistoricalCollector(self.gate.rest, self.settings)
                for point in points:
                    if cancel_event.is_set():
                        job["status"] = "cancelled"
                        break
                    job["phase"] = "collecting"
                    job["current_timepoint"] = point.isoformat()
                    await self.repository.save_replay(job)
                    result = await self._timepoint(collector, point, job["request"], cancel_event)
                    job["results"].append(result)
                    job["completed"] += 1
                    if result.get("reliable"):
                        job["diagnostics"]["reliable_timepoints"] += 1
                    else:
                        job["diagnostics"]["unreliable_timepoints"] += 1
                    point_diagnostics = result.get("diagnostics", {})
                    job["diagnostics"]["api_errors"] += len(point_diagnostics.get("api_errors", []))
                    job["diagnostics"]["indicator_errors"] += len(point_diagnostics.get("indicator_failures", []))
                    job["phase"] = "timepoint_completed"
                    await self.repository.save_replay(job)
                else:
                    job["status"] = "completed"
                    job["phase"] = "completed"
                    if self.notifier and job["request"].get("send_discord"):
                        await self.notifier.send_replay(job, "all" in job["request"].get("ranking_types", ["combined"]))
            except asyncio.CancelledError:
                job["status"] = "cancelled"
                await self.repository.save_replay(job)
            except Exception as exc:
                job["status"] = "failed"
                job["diagnostics"]["api_errors"] += 1
                job["diagnostics"]["error"] = type(exc).__name__
                job["diagnostics"]["error_message"] = str(exc)
                logger.exception("replay job failed")
            finally:
                await self.repository.save_replay(job)

    async def _timepoint(self, collector: HistoricalCollector, point: datetime, request: dict[str, Any], cancel_event: asyncio.Event) -> dict[str, Any]:
        diagnostics = new_diagnostics(point.isoformat(), point.isoformat())
        try:
            universe = await collector.collect_universe(point)
        except Exception as exc:
            diagnostics["api_errors"].append(type(exc).__name__)
            diagnostics["ranking_suppression_reasons"].append("universe_unavailable")
            return {"requested_time": point.isoformat(), "aligned_time": point.isoformat(), "rankings": {"combined": [], "long": [], "short": []}, "diagnostics": diagnostics, "reliable": False}
        diagnostics["universe_total"] = len(universe)
        items: list[dict[str, Any]] = []
        for raw in universe:
            if cancel_event.is_set():
                break
            try:
                bundle = await collector.collect_contract(raw, point, request.get("include_details", True))
                analysis = build_features(bundle, self.settings.min_30m_candles, self.settings.min_4h_candles)
                item = run_snapshot(analysis, self.settings)
                if item and item.get("qualifies"):
                    items.append(item)
                else:
                    diagnostics["data_completeness_failures"].append(raw.get("name", "unknown"))
                    diagnostics["ranking_suppression_reasons"].append(f"{raw.get('name','unknown')}:unreliable_data")
                for name in analysis.get("missing_data", []):
                    field = "missing_oi" if name == "oi" else "missing_funding" if name == "funding" else "missing_candle_data" if name in {"4h", "30m", "15m", "5m"} else "missing_active_buy_sell" if name == "active_trade_aggregate" else "missing_liquidation" if name == "liquidation" else None
                    if field:
                        diagnostics[field].append(raw.get("name", "unknown"))
            except Exception as exc:
                diagnostics["indicator_failures"].append({"contract": raw.get("name", "unknown"), "error_type": type(exc).__name__, "error": str(exc)})
        from app.scanner.ranking import build_rankings
        rankings = build_rankings(items, request.get("top_n", 10))
        diagnostics["contracts_available"] = len(universe)
        diagnostics["contracts_ranked"] = len(rankings["combined"])
        diagnostics["contracts_excluded"] = max(0, len(universe) - len(items))
        diagnostics["reliable"] = bool(rankings["combined"])
        if not diagnostics["reliable"]:
            diagnostics["ranking_suppression_reasons"].append("此時間點無可靠排名")
        return {"requested_time": point.isoformat(), "aligned_time": point.isoformat(), "rankings": rankings, "diagnostics": diagnostics, "reliable": diagnostics["reliable"]}

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        return await self.repository.get_replay(job_id)

    async def cancel(self, job_id: str) -> bool:
        event = self.cancel_events.get(job_id)
        job = await self.repository.get_replay(job_id)
        if not job:
            return False
        if event:
            event.set()
        elif job.get("status") in {"queued", "starting", "cancelling"}:
            job["status"] = "cancelled"
            job["phase"] = "cancelled_before_start"
            await self.repository.save_replay(job)
            return True
        job["status"] = "cancelling"
        await self.repository.save_replay(job)
        return True
