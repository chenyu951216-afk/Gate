import asyncio

from app.config import get_settings
from app.gate.rest_client import GateRestClient


async def main() -> None:
    client = GateRestClient(get_settings())
    try:
        contracts = await client.get_contracts()
        tickers = await client.get_tickers()
        print({"contracts": len(contracts), "tickers": len(tickers), "status": "ok"})
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())

