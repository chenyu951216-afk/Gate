import copy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select, text

from app.database.models import ManagedPosition, OrderEvent, ReplayJob, ScanRun, TradingControl


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _datetime_value(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


class MemoryRepository:
    mode = "memory"

    def __init__(self):
        self.scans: list[dict[str, Any]] = []
        self.replays: dict[str, dict[str, Any]] = {}
        self.notifications: list[dict[str, Any]] = []
        self.backtests: dict[str, dict[str, Any]] = {}
        self.managed_positions: dict[str, dict[str, Any]] = {}
        self.order_events: dict[str, dict[str, Any]] = {}
        self.trading_paused = False
        self.trading_pause_reason: str | None = None
        self.trading_mode = "live"

    async def save_scan(self, result: dict[str, Any]) -> None:
        self.scans.append(copy.deepcopy(_json_safe(result)))
        self.scans = self.scans[-100:]

    async def latest_scan(self) -> dict[str, Any] | None:
        return copy.deepcopy(self.scans[-1]) if self.scans else None

    async def scan_history(self, limit: int = 50) -> list[dict[str, Any]]:
        return copy.deepcopy(self.scans[-limit:][::-1])

    async def save_replay(self, job: dict[str, Any]) -> None:
        self.replays[job["job_id"]] = copy.deepcopy(_json_safe(job))

    async def get_replay(self, job_id: str) -> dict[str, Any] | None:
        value = self.replays.get(job_id)
        return copy.deepcopy(value) if value else None

    async def save_notification(self, delivery: dict[str, Any]) -> None:
        self.notifications.append(copy.deepcopy(_json_safe(delivery)))
        self.notifications = self.notifications[-200:]

    async def notification_history(self, limit: int = 50) -> list[dict[str, Any]]:
        return copy.deepcopy(self.notifications[-limit:][::-1])

    async def save_backtest(self, result: dict[str, Any]) -> None:
        self.backtests[result["run_id"]] = copy.deepcopy(_json_safe(result))

    async def save_managed_position(self, position: dict[str, Any]) -> None:
        key = position["position_key"]
        self.managed_positions[key] = copy.deepcopy(_json_safe(position))

    async def get_managed_position(self, position_key: str) -> dict[str, Any] | None:
        value = self.managed_positions.get(position_key)
        return copy.deepcopy(value) if value else None

    async def list_managed_positions(self, active_only: bool = False) -> list[dict[str, Any]]:
        values = list(self.managed_positions.values())
        if active_only:
            values = [value for value in values if value.get("status") == "active"]
        return copy.deepcopy(values)

    async def delete_managed_position(self, position_key: str) -> None:
        self.managed_positions.pop(position_key, None)

    async def save_order_event(self, event: dict[str, Any]) -> None:
        self.order_events[event["event_id"]] = copy.deepcopy(_json_safe(event))

    async def get_trading_control(self) -> dict[str, Any]:
        return {"paused": self.trading_paused, "reason": self.trading_pause_reason, "mode": self.trading_mode}

    async def set_trading_paused(self, paused: bool, reason: str | None = None) -> dict[str, Any]:
        self.trading_paused = paused
        self.trading_pause_reason = reason
        return await self.get_trading_control()

    async def set_trading_mode(self, mode: str) -> dict[str, Any]:
        normalized = str(mode).lower()
        if normalized in {"formal", "production", "real"}:
            normalized = "live"
        if normalized not in {"live", "test"}:
            raise ValueError("mode must be live or test")
        self.trading_mode = normalized
        return await self.get_trading_control()


class PostgresRepository(MemoryRepository):
    mode = "postgresql"

    def __init__(self, session_factory: Any):
        super().__init__()
        self.session_factory = session_factory

    async def save_scan(self, result: dict[str, Any]) -> None:
        await super().save_scan(result)
        payload = _json_safe(result)
        async with self.session_factory() as session:
            session.add(
                ScanRun(
                    scan_id=result["scan_id"], status=result["status"], started_at=result["started_at"],
                    finished_at=result.get("finished_at"), elapsed_seconds=result.get("elapsed_seconds"),
                    universe_total=result.get("universe_total", 0), payload=payload,
                )
            )
            await session.commit()

    async def save_replay(self, job: dict[str, Any]) -> None:
        await super().save_replay(job)
        async with self.session_factory() as session:
            existing = await session.get(ReplayJob, job["job_id"])
            if existing is None:
                session.add(ReplayJob(job_id=job["job_id"], status=job["status"], created_at=job["created_at"], payload=_json_safe(job)))
            else:
                existing.status = job["status"]
                existing.payload = _json_safe(job)
            await session.commit()

    async def save_managed_position(self, position: dict[str, Any]) -> None:
        await super().save_managed_position(position)
        payload = _json_safe(position)
        async with self.session_factory() as session:
            row = await session.get(ManagedPosition, position["position_key"])
            if row is None:
                session.add(
                    ManagedPosition(
                        position_key=position["position_key"],
                        contract=position["contract"],
                        side=position["side"],
                        status=position.get("status", "active"),
                        updated_at=_datetime_value(position.get("updated_at")),
                        payload=payload,
                    )
                )
            else:
                row.contract = position["contract"]
                row.side = position["side"]
                row.status = position.get("status", "active")
                row.updated_at = _datetime_value(position.get("updated_at"))
                row.payload = payload
            await session.commit()

    async def get_managed_position(self, position_key: str) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            row = await session.get(ManagedPosition, position_key)
            if row is None:
                return await super().get_managed_position(position_key)
            value = dict(row.payload or {})
            self.managed_positions[position_key] = value
            return copy.deepcopy(value)

    async def list_managed_positions(self, active_only: bool = False) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            statement = select(ManagedPosition)
            if active_only:
                statement = statement.where(ManagedPosition.status == "active")
            rows = (await session.execute(statement)).scalars().all()
            values = [dict(row.payload or {}) for row in rows]
            for value in values:
                if value.get("position_key"):
                    self.managed_positions[value["position_key"]] = value
            return copy.deepcopy(values)

    async def delete_managed_position(self, position_key: str) -> None:
        await super().delete_managed_position(position_key)
        async with self.session_factory() as session:
            await session.execute(delete(ManagedPosition).where(ManagedPosition.position_key == position_key))
            await session.commit()

    async def save_order_event(self, event: dict[str, Any]) -> None:
        await super().save_order_event(event)
        async with self.session_factory() as session:
            row = await session.get(OrderEvent, event["event_id"])
            if row is None:
                session.add(
                    OrderEvent(
                        event_id=event["event_id"],
                        client_order_id=event["client_order_id"],
                        contract=event["contract"],
                        event_type=event["event_type"],
                        created_at=event["created_at"],
                        payload=_json_safe(event),
                    )
                )
            else:
                row.payload = _json_safe(event)
            await session.commit()

    async def get_trading_control(self) -> dict[str, Any]:
        async with self.session_factory() as session:
            row = await session.get(TradingControl, 1)
            if row is None:
                row = TradingControl(id=1, paused=False, mode="live", updated_at=datetime.now(timezone.utc))
                session.add(row)
                await session.commit()
            self.trading_paused = bool(row.paused)
            self.trading_pause_reason = row.reason
            self.trading_mode = str(row.mode or "live")
            return {"paused": self.trading_paused, "reason": self.trading_pause_reason, "mode": self.trading_mode}

    async def set_trading_paused(self, paused: bool, reason: str | None = None) -> dict[str, Any]:
        async with self.session_factory() as session:
            row = await session.get(TradingControl, 1)
            if row is None:
                row = TradingControl(id=1, paused=paused, reason=reason, mode=self.trading_mode, updated_at=datetime.now(timezone.utc))
                session.add(row)
            else:
                row.paused = paused
                row.reason = reason
                row.updated_at = datetime.now(timezone.utc)
            await session.commit()
        return await super().set_trading_paused(paused, reason)

    async def set_trading_mode(self, mode: str) -> dict[str, Any]:
        normalized = str(mode).lower()
        if normalized in {"formal", "production", "real"}:
            normalized = "live"
        if normalized not in {"live", "test"}:
            raise ValueError("mode must be live or test")
        async with self.session_factory() as session:
            row = await session.get(TradingControl, 1)
            if row is None:
                row = TradingControl(id=1, paused=False, reason=None, mode=normalized, updated_at=datetime.now(timezone.utc))
                session.add(row)
            else:
                row.mode = normalized
                row.updated_at = datetime.now(timezone.utc)
            await session.commit()
        return await super().set_trading_mode(normalized)

    async def try_advisory_lock(self, key: int = 20260711) -> bool:
        async with self.session_factory() as session:
            row = await session.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            return bool(row.scalar())
