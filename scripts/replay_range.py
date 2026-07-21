import argparse
import asyncio

from app.config import get_settings
from app.database.repository import MemoryRepository
from app.gate.client import GateClient
from app.gate.rest_client import GateRestClient
from app.gate.websocket_client import GateFuturesWebsocket
from app.replay.engine import ReplayService


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("start_time")
    parser.add_argument("end_time")
    args = parser.parse_args()
    settings = get_settings()
    rest = GateRestClient(settings)
    gate = GateClient(rest, GateFuturesWebsocket(settings.gate_ws_url))
    try:
        repository = MemoryRepository()
        service = ReplayService(gate, repository, settings)
        job = await service.create_job({"start_time": args.start_time, "end_time": args.end_time, "timezone": settings.timezone, "interval_minutes": 30, "top_n": 10, "ranking_types": ["combined"], "include_details": True, "send_discord": False})
        await service.tasks[job["job_id"]]
        print(await service.get_job(job["job_id"]))
    finally:
        await gate.close()


if __name__ == "__main__":
    asyncio.run(main())

