from app.bitget.rest_client import BitgetRestClient


class BitgetClient:
    """Small container matching the old Gate client shape."""

    def __init__(self, rest: BitgetRestClient):
        self.rest = rest

    async def close(self) -> None:
        await self.rest.close()
