import httpx
import pytest
from app.gate.rest_client import GateRestClient

@pytest.mark.asyncio
async def test_gate_429_retry(settings):
    calls = 0
    def handler(request):
        nonlocal calls; calls += 1; return httpx.Response(429 if calls == 1 else 200, json=[])
    client = GateRestClient(settings, transport=httpx.MockTransport(handler)); result = await client.get_contracts(); await client.close(); assert result == []; assert calls == 2


@pytest.mark.asyncio
async def test_private_json_request_signs_body_and_decimal_sizes(settings):
    settings.gate_api_key = "key"
    settings.gate_api_secret = "secret"
    seen = {}

    def handler(request):
        seen["body"] = request.content
        seen["signature"] = request.headers.get("SIGN")
        seen["decimal"] = request.headers.get("X-Gate-Size-Decimal")
        return httpx.Response(201, json={"id": 123})

    client = GateRestClient(settings, transport=httpx.MockTransport(handler))
    result = await client.place_futures_order({"contract": "BTC_USDT", "size": "1", "price": "0", "tif": "ioc"})
    await client.close()
    assert result["id"] == 123
    assert b'"contract":"BTC_USDT"' in seen["body"]
    assert seen["signature"]
    assert seen["decimal"] == "1"
