import asyncio
import random
import time


class AsyncRateLimiter:
    def __init__(self, requests_per_second: float):
        self.interval = 1.0 / max(requests_per_second, 0.1)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            wait_for = self.interval - (time.monotonic() - self._last)
            if wait_for > 0:
                await asyncio.sleep(wait_for + random.uniform(0, min(wait_for * 0.2, 0.05)))
            self._last = time.monotonic()

