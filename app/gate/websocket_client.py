import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import websockets

logger = logging.getLogger(__name__)


class GateFuturesWebsocket:
    def __init__(self, url: str, reconnect_seconds: float = 5.0):
        self.url = url
        self.reconnect_seconds = reconnect_seconds

    async def stream(self, channel: str, payload: list[str]) -> AsyncIterator[dict[str, Any]]:
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=20, ping_timeout=20) as socket:
                    message = {
                        "time": int(time.time()),
                        "channel": channel,
                        "event": "subscribe",
                        "payload": payload,
                    }
                    await socket.send(json.dumps(message))
                    async for raw in socket:
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if data.get("event") == "update":
                            yield data
            except (OSError, asyncio.CancelledError):
                raise
            except Exception as exc:
                logger.warning("Gate websocket reconnect after %s", type(exc).__name__)
                await asyncio.sleep(self.reconnect_seconds)

