import asyncio

from app.config import get_settings
from app.database.repository import MemoryRepository
from app.gate.client import GateClient
from app.gate.rest_client import GateRestClient
from app.gate.websocket_client import GateFuturesWebsocket
from app.scanner.service import ScanService


async def main() -> None:
    settings = get_settings()
    repository = MemoryRepository()
    rest = GateRestClient(settings)
    gate = GateClient(rest, GateFuturesWebsocket(settings.gate_ws_url))
    try:
        result = await ScanService(gate, repository, settings).run(dry_run=True, notify_discord=False)
        print(result)
    finally:
        await gate.close()


if __name__ == "__main__":
    asyncio.run(main())

