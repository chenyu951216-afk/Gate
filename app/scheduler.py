import asyncio
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


class ScanScheduler:
    def __init__(self, scanner, settings):
        self.scanner = scanner
        self.settings = settings
        self.task: asyncio.Task | None = None
        self.running = False
        self._last_slot: datetime | None = None
        self.next_scan_at: datetime | None = None
        self.last_scan_started_at: datetime | None = None
        self.last_scan_finished_at: datetime | None = None
        self.last_scan_status: str | None = None
        self.last_scan_error: str | None = None

    def start(self) -> None:
        if self.task is None:
            self.running = True
            self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            finally:
                self.task = None

    async def _run_scan(self, reason: str) -> None:
        self.last_scan_started_at = datetime.now(timezone.utc)
        self.last_scan_error = None
        try:
            result = await self.scanner.run(notify_discord=True)
            self.last_scan_status = str(result.get("status", "completed"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_scan_status = "failed"
            self.last_scan_error = f"{reason}:{type(exc).__name__}: {exc}"
            logger.exception("%s scan failed", reason)
        finally:
            self.last_scan_finished_at = datetime.now(timezone.utc)

    async def _loop(self) -> None:
        if self.settings.scan_on_startup:
            await self._run_scan("startup")
        while self.running:
            now = datetime.now(timezone.utc)
            next_slot = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0) + timedelta(minutes=30)
            if self._last_slot == next_slot:
                await asyncio.sleep(5)
                continue
            delay = max(1, (next_slot - now).total_seconds() + self.settings.scan_delay_seconds)
            self.next_scan_at = now + timedelta(seconds=delay)
            await asyncio.sleep(delay)
            self._last_slot = next_slot
            await self._run_scan("scheduled")
