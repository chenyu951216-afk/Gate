import json

import httpx
import pytest

from app.bitget.rest_client import BitgetRestClient
from app.exceptions import GateAPIError


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
                "minTradeUSDT": "5",
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
    assert contracts[0]["order_notional_min"] == 5
    assert tickers[0]["contract"] == "ZEC_USDT"
    assert tickers[0]["volume_24h_quote"] == "8000000"
    assert candles[0]["t"] == 1700000000


async def test_bitget_order_book_normalizes_depth_and_notionals(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/market/merge-depth")
        return httpx.Response(200, json={"code": "00000", "data": {
            "bids": [["99.9", "10"]], "asks": [["100.1", "12"]], "ts": "1700000000000",
        }})

    client = BitgetRestClient(settings, transport=httpx.MockTransport(handler))
    book = await client.get_order_book("BTC_USDT")
    await client.close()
    assert book["bids"][0]["notional"] == 999
    assert book["asks"][0]["notional"] == pytest.approx(1201.2)


async def test_bitget_leverage_adapter_floors_decimal_to_integer(settings):
    settings.bitget_api_key = "key"
    settings.bitget_api_secret = "secret"
    settings.bitget_api_passphrase = "pass"
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"code": "00000", "data": {
            "crossMarginLeverage": "1",
            "marginMode": "crossed",
        }})

    client = BitgetRestClient(settings, transport=httpx.MockTransport(handler))
    result = await client.set_leverage("BANK_USDT", 1.6090931657010321, "cross")
    await client.close()
    assert captured["leverage"] == "1"
    assert result["cross_leverage_limit"] == "1"


async def test_bitget_max_open_preflight_uses_limit_price_and_side(settings):
    settings.bitget_api_key = "key"
    settings.bitget_api_secret = "secret"
    settings.bitget_api_passphrase = "pass"
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={"code": "00000", "data": {"maxOpen": "12.34"}},
        )

    client = BitgetRestClient(settings, transport=httpx.MockTransport(handler))
    maximum = await client.get_max_openable_quantity(
        "BANK_USDT",
        "long",
        1.609,
    )
    await client.close()
    assert maximum == 12.34
    assert captured["posSide"] == "long"
    assert captured["orderType"] == "limit"
    assert captured["openPrice"] == "1.609"


async def test_authenticated_retry_refreshes_timestamp_and_signature(settings, monkeypatch):
    settings.bitget_api_key = "key"
    settings.bitget_api_secret = "secret"
    settings.bitget_api_passphrase = "pass"
    timestamp = [1000.0]

    def advancing_time():
        timestamp[0] += 1.0
        return timestamp[0]

    monkeypatch.setattr(
        "app.bitget.rest_client.time.time",
        advancing_time,
    )
    captured: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (
                request.headers["ACCESS-TIMESTAMP"],
                request.headers["ACCESS-SIGN"],
            )
        )
        if len(captured) == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "0.001"},
                json={"code": "429", "msg": "rate limited"},
            )
        return httpx.Response(200, json={"code": "00000", "data": []})

    client = BitgetRestClient(settings, transport=httpx.MockTransport(handler))
    await client.get_account()
    await client.close()
    assert captured[0][0] != captured[1][0]
    assert captured[0][1] != captured[1][1]


async def test_order_preflight_blocks_below_exchange_minimum_notional(settings):
    client = BitgetRestClient(
        settings,
        transport=httpx.MockTransport(
            lambda request: pytest.fail("invalid order must not reach HTTP")
        ),
    )
    client._contracts["BANK_USDT"] = {
        "status": "trading",
        "sizeMultiplier": "0.01",
        "order_size_min": "0.01",
        "order_notional_min": "5",
        "order_price_round": "0.001",
        "raw": {
            "sizeMultiplier": "0.01",
            "minTradeNum": "0.01",
            "minTradeUSDT": "5",
            "maxOrderQty": "1000",
        },
    }
    with pytest.raises(GateAPIError, match="below minimum"):
        await client.place_futures_order(
            {
                "contract": "BANK_USDT",
                "size": "0.01",
                "price": "100",
                "tif": "gtc",
                "reduce_only": False,
            }
        )
    await client.close()


async def test_parameter_error_does_not_open_global_exchange_circuit(settings):
    attempts = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        attempts[0] += 1
        if attempts[0] <= settings.bitget_circuit_failure_threshold:
            return httpx.Response(
                200,
                json={
                    "code": "40020",
                    "msg": "Parameter error",
                    "data": None,
                },
            )
        return httpx.Response(
            200,
            json={
                "code": "00000",
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "lastPr": "100",
                        "markPrice": "100",
                    }
                ],
            },
        )

    client = BitgetRestClient(settings, transport=httpx.MockTransport(handler))
    for _ in range(settings.bitget_circuit_failure_threshold):
        with pytest.raises(GateAPIError, match="40020"):
            await client.get_ticker("BANK_USDT")
    ticker = await client.get_ticker("BTC_USDT")
    await client.close()
    assert ticker["contract"] == "BTC_USDT"


async def test_bitget_fills_expose_realized_pnl_fees_and_net(settings):
    settings.bitget_api_key = "key"
    settings.bitget_api_secret = "secret"
    settings.bitget_api_passphrase = "pass"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/order/fills")
        return httpx.Response(200, json={"code": "00000", "data": {"fillList": [{
            "tradeId": "t1", "orderId": "o1", "symbol": "ESPUSDT",
            "price": "0.1", "baseVolume": "1000", "quoteVolume": "100",
            "profit": "-1.2", "feeDetail": [{"totalFee": "-0.06"}],
            "side": "sell", "tradeSide": "reduce_sell_single", "cTime": "1700000000000",
        }]}})

    client = BitgetRestClient(settings, transport=httpx.MockTransport(handler))
    fills = await client.get_order_fills(limit=100)
    await client.close()
    assert fills[0]["contract"] == "ESP_USDT"
    assert fills[0]["realized_pnl"] == -1.2
    assert fills[0]["fee"] == -0.06
    assert fills[0]["net_pnl"] == pytest.approx(-1.26)


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
        "tpsl_tp_trigger_price": "110",
    })
    await client.close()
    assert response["id"] == "123"
    assert captured == {
        "symbol": "ZECUSDT", "productType": "USDT-FUTURES", "marginMode": "crossed",
        "marginCoin": "USDT", "size": "2.5", "side": "buy", "orderType": "limit",
        "reduceOnly": "NO", "clientOid": "t-auto-entry-test", "price": "100", "force": "gtc",
        "presetStopLossPrice": "95", "presetStopSurplusPrice": "110",
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


async def test_bitget_account_preserves_official_position_and_margin_modes(settings):
    settings.bitget_api_key = "key"
    settings.bitget_api_secret = "secret"
    settings.bitget_api_passphrase = "pass"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/account/accounts"):
            return httpx.Response(200, json={"code": "00000", "data": [{
                "marginCoin": "USDT", "accountEquity": "1000", "available": "1000",
                "posMode": "one_way_mode", "marginMode": "crossed",
            }]})
        return httpx.Response(404, json={"code": "404", "msg": "not found"})

    client = BitgetRestClient(settings, transport=httpx.MockTransport(handler))
    account = await client.get_account()
    await client.close()
    assert account["position_mode"] == "single"
    assert account["in_dual_mode"] is False
    assert account["pos_margin_mode"] == "cross"


def test_bitget_contract_identity_does_not_fuzzy_match_multiplier_symbols():
    from app.bitget.rest_client import BitgetRestClient

    assert BitgetRestClient.contract_identity("BTC_USDT") == ("BTC", "USDT")
    assert BitgetRestClient.contract_identity("BTCUSDT") == ("BTC", "USDT")
    assert BitgetRestClient.contract_identity("1000BONK_USDT") == ("1000BONK", "USDT")
    assert BitgetRestClient.contract_identity("BONK_USDT") != BitgetRestClient.contract_identity("1000BONK_USDT")
