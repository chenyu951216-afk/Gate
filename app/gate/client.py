from app.gate.rest_client import GateRestClient
from app.gate.websocket_client import GateFuturesWebsocket


class GateClient:
    def __init__(self, rest: GateRestClient, websocket: GateFuturesWebsocket):
        self.rest = rest
        self.websocket = websocket

    async def close(self) -> None:
        await self.rest.close()

