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


@pytest.mark.asyncio
async def test_cross_leverage_queries_use_gate_numeric_format(settings):
    settings.gate_api_key = "key"
    settings.gate_api_secret = "secret"
    queries = []

    def handler(request):
        queries.append(str(request.url.query))
        return httpx.Response(200, json={"pos_margin_mode": "cross", "leverage": "0"})

    client = GateRestClient(settings, transport=httpx.MockTransport(handler))
    await client.set_leverage("BTC_USDT", 100.0, "cross")
    await client.set_cross_leverage_legacy("BTC_USDT", 40.0)
    await client.close()
    assert "leverage=100.0" not in queries[0]
    assert "leverage=100" in queries[0]
    assert "cross_leverage_limit=40" in queries[1]
