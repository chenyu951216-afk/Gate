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
                return
            self.task = None

    async def _loop(self) -> None:
        if self.settings.scan_on_startup:
            await self.scanner.run(notify_discord=True)
        while self.running:
            now = datetime.now(timezone.utc)
            next_slot = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0) + timedelta(minutes=30)
            if self._last_slot == next_slot:
                await asyncio.sleep(5)
                continue
            delay = max(1, (next_slot - now).total_seconds() + self.settings.scan_delay_seconds)
            await asyncio.sleep(delay)
            self._last_slot = next_slot
            try:
                await self.scanner.run(notify_discord=True)
            except Exception:
                logger.exception("scheduled scan failed")
