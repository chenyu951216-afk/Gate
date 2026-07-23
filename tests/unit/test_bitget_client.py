import json

import httpx

from app.bitget.rest_client import BitgetRestClient


async def test_bitget_contract_ticker_and_candle_normalization(settings):
    settings.bitget_api_key = "key"
    settings.bitget_api_secret = "secret"
    settings.bitget_api_passphrase = "pass"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/market/contracts"):
            return httpx.Response(200, json={"code": "00000", "data": [{
                "symbol": "ZECUSDT", "symbolStatus": "normal", "minTradeNum": "0.01",
                "sizeMultiplier": "0.01", "priceEndStep": "1", "pricePlace": "4",
                "minLever": "1", "maxLever": "50", "maxOrderQty": "1000",
            }]})
        if request.url.path.endswith("/market/tickers"):
            return httpx.Response(200, json={"code": "00000", "data": [{
                "symbol": "ZECUSDT", "lastPr": "100", "bidPr": "99.9", "askPr": "100.1",
                "markPrice": "100", "indexPrice": "100", "usdtVolume": "8000000",
                "holdingAmount": "10", "change24h": "0.01",
            }]})
        if request.url.path.endswith("/market/candles"):
            return httpx.Response(200, json={"code": "00000", "data": [["1700000000000", "1", "2", "0.5", "1.5", "10"]]})
        return httpx.Response(404, json={"code": "404", "msg": "not found"})

    client = BitgetRestClient(settings, transport=httpx.MockTransport(handler))
    contracts = await client.get_contracts()
    tickers = await client.get_tickers()
    candles = await client.get_candlesticks("ZEC_USDT", "30m", limit=1)
    await client.close()
    assert contracts[0]["name"] == "ZEC_USDT"
    assert contracts[0]["leverage_max"] == 50
    assert contracts[0]["order_price_round"] == 0.0001
    assert tickers[0]["contract"] == "ZEC_USDT"
    assert tickers[0]["volume_24h_quote"] == "8000000"
    assert candles[0]["t"] == 1700000000


async def test_bitget_place_order_uses_one_way_cross_and_base_coin_size(settings):
    settings.bitget_api_key = "key"
    settings.bitget_api_secret = "secret"
    settings.bitget_api_passphrase = "pass"
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/order/place-order"):
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"code": "00000", "data": {"orderId": "123", "clientOid": "client"}})
        return httpx.Response(404, json={"code": "404", "msg": "not found"})

    client = BitgetRestClient(settings, transport=httpx.MockTransport(handler))
    response = await client.place_futures_order({
        "contract": "ZEC_USDT", "size": "2.5", "price": "100", "tif": "gtc",
        "text": "t-auto-entry-test", "reduce_only": False,
        "tpsl_sl_trigger_price": "95",
    })
    await client.close()
    assert response["id"] == "123"
    assert captured == {
        "symbol": "ZECUSDT", "productType": "USDT-FUTURES", "marginMode": "crossed",
        "marginCoin": "USDT", "size": "2.5", "side": "buy", "orderType": "limit",
        "reduceOnly": "NO", "clientOid": "t-auto-entry-test", "price": "100", "force": "gtc",
        "presetStopLossPrice": "95", "presetStopLossExecutePrice": "0",
    }


async def test_bitget_exchange_tpsl_uses_one_way_hold_side_and_size_step(settings):
    settings.bitget_api_key = "key"
    settings.bitget_api_secret = "secret"
    settings.bitget_api_passphrase = "pass"
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/position/all-position"):
            return httpx.Response(200, json={"code": "00000", "data": [{
                "symbol": "ZECUSDT", "total": "2.50", "holdSide": "long", "openPriceAvg": "100",
                "marginMode": "crossed", "posMode": "one_way_mode", "leverage": "50",
            }]})
        if request.url.path.endswith("/order/place-tpsl-order"):
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"code": "00000", "data": {"orderId": "sl-1", "clientOid": "sl"}})
        return httpx.Response(404, json={"code": "404", "msg": "not found"})

    client = BitgetRestClient(settings, transport=httpx.MockTransport(handler))
    client._contracts["ZEC_USDT"] = {"raw": {"sizeMultiplier": "0.01"}}
    response = await client.create_price_order({
        "initial": {"contract": "ZEC_USDT", "size": 0, "text": "t-auto-sl-test"},
        "trigger": {"price": "95", "price_type": 0},
        "order_type": "close-long-position",
    })
    await client.close()
    assert response["id"] == "sl-1"
    assert captured["symbol"] == "ZECUSDT"
    assert captured["holdSide"] == "buy"
    assert captured["size"] == "2.5"
    assert captured["planType"] == "loss_plan"


async def test_bitget_cancel_tpsl_uses_actual_plan_type(settings):
    settings.bitget_api_key = "key"
    settings.bitget_api_secret = "secret"
    settings.bitget_api_passphrase = "pass"
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/order/orders-plan-pending"):
            return httpx.Response(200, json={"code": "00000", "data": {"entrustedList": [{
                "symbol": "ZECUSDT", "orderId": "tp-1", "planType": "profit_plan",
                "planStatus": "live", "posSide": "net", "side": "buy", "size": "2.5",
                "triggerPrice": "110", "triggerType": "mark_price",
            }]}})
        if request.url.path.endswith("/order/cancel-plan-order"):
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"code": "00000", "data": {"successList": [{"orderId": "tp-1"}]}})
        return httpx.Response(404, json={"code": "404", "msg": "not found"})

    client = BitgetRestClient(settings, transport=httpx.MockTransport(handler))
    response = await client.cancel_price_order("tp-1")
    await client.close()
    assert response["successList"][0]["orderId"] == "tp-1"
    assert captured["planType"] == "profit_plan"


def test_bitget_contract_identity_does_not_fuzzy_match_multiplier_symbols():
    from app.bitget.rest_client import BitgetRestClient

    assert BitgetRestClient.contract_identity("BTC_USDT") == ("BTC", "USDT")
    assert BitgetRestClient.contract_identity("BTCUSDT") == ("BTC", "USDT")
    assert BitgetRestClient.contract_identity("1000BONK_USDT") == ("1000BONK", "USDT")
    assert BitgetRestClient.contract_identity("BONK_USDT") != BitgetRestClient.contract_identity("1000BONK_USDT")
