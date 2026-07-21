import argparse
import asyncio

from app.backtest.engine import BacktestService
from app.config import get_settings
from app.database.repository import MemoryRepository


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_file")
    args = parser.parse_args()
    import json
    with open(args.job_file, encoding="utf-8") as file:
        job = json.load(file)
    result = await BacktestService(MemoryRepository(), get_settings()).run(job, {"ranking_types": ["combined"], "top_n": 10, "holding_bars": 4})
    print(result)


if __name__ == "__main__":
    asyncio.run(main())

