import hashlib
import hmac
import re
from urllib.parse import parse_qs, urlparse

import pytest
from aioresponses import aioresponses

from aitos.execution.binance_executor import (MAINNET_URL, TESTNET_URL,
                                              BinanceFuturesOrderExecutor,
                                              round_step)
from aitos.models.trade import TradeSide


def test_round_step_truncates_to_precision():
    assert round_step(1.23456, 2) == 1.23
    assert round_step(1.999, 0) == 1.0
    assert round_step(0.001234, 3) == 0.001


def test_defaults_to_testnet_for_safety():
    executor = BinanceFuturesOrderExecutor(api_key="key", api_secret="secret")
    assert executor._base_url == TESTNET_URL


def test_explicit_mainnet_opt_in():
    executor = BinanceFuturesOrderExecutor(
        api_key="key", api_secret="secret", testnet=False
    )
    assert executor._base_url == MAINNET_URL


def test_missing_credentials_raises():
    with pytest.raises(ValueError):
        BinanceFuturesOrderExecutor(api_key="", api_secret="")


@pytest.mark.asyncio
async def test_submit_order_signs_request_correctly():
    executor = BinanceFuturesOrderExecutor(api_key="test-key", api_secret="test-secret")
    await executor.connect()

    captured_url = {}

    def callback(url, **kwargs):
        captured_url["url"] = str(url)
        captured_url["headers"] = kwargs.get("headers", {})

    with aioresponses() as m:
        m.post(
            re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v1/order") + r".*"),
            payload={
                "orderId": 12345,
                "status": "FILLED",
                "executedQty": "1.0",
                "avgPrice": "100.5",
            },
            callback=callback,
        )
        result = await executor.submit_order(_order_request())

    assert result.success is True
    assert result.order_id == "12345"
    assert result.fill_price == 100.5
    assert captured_url["headers"]["X-MBX-APIKEY"] == "test-key"

    parsed = urlparse(captured_url["url"])
    query = parse_qs(parsed.query)
    assert "signature" in query
    assert "timestamp" in query
    assert "recvWindow" in query

    await executor.close()


def test_sign_produces_correct_hmac_sha256_signature():
    executor = BinanceFuturesOrderExecutor(api_key="test-key", api_secret="test-secret")
    params = {"symbol": "BTCUSDT", "side": "BUY", "timestamp": 1234567890}

    signed_query = executor._sign(params)

    query_string, signature = signed_query.rsplit("&signature=", 1)
    expected_signature = hmac.new(
        b"test-secret", query_string.encode(), hashlib.sha256
    ).hexdigest()
    assert signature == expected_signature
    assert query_string == "symbol=BTCUSDT&side=BUY&timestamp=1234567890"


@pytest.mark.asyncio
async def test_submit_order_maps_long_to_buy_and_short_to_sell():
    executor = BinanceFuturesOrderExecutor(api_key="k", api_secret="s")
    await executor.connect()

    for side, expected in [(TradeSide.LONG, "BUY"), (TradeSide.SHORT, "SELL")]:
        captured = {}

        def callback(url, **kwargs):
            captured["url"] = str(url)

        with aioresponses() as m:
            m.post(
                re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v1/order") + r".*"),
                payload={
                    "orderId": 1,
                    "status": "FILLED",
                    "executedQty": "1.0",
                    "avgPrice": "100.0",
                },
                callback=callback,
            )
            await executor.submit_order(_order_request(side=side))

        query = parse_qs(urlparse(captured["url"]).query)
        assert query["side"][0] == expected

    await executor.close()


@pytest.mark.asyncio
async def test_submit_order_returns_failure_result_on_api_error():
    executor = BinanceFuturesOrderExecutor(api_key="k", api_secret="s")
    await executor.connect()

    with aioresponses() as m:
        m.post(
            re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v1/order") + r".*"),
            status=400,
            payload={"code": -2019, "msg": "Margin is insufficient"},
        )
        result = await executor.submit_order(_order_request())

    assert result.success is False
    assert "-2019" in result.error
    assert "Margin is insufficient" in result.error

    await executor.close()


@pytest.mark.asyncio
async def test_submit_order_before_connect_raises():
    executor = BinanceFuturesOrderExecutor(api_key="k", api_secret="s")
    with pytest.raises(RuntimeError):
        await executor.submit_order(_order_request())


@pytest.mark.asyncio
async def test_submit_order_applies_symbol_filter_quantity_rounding():
    from aitos.exchange.symbol_filters import SymbolFilters

    filters = {
        "BTCUSDT": SymbolFilters(
            symbol="BTCUSDT",
            step_size=0.001,
            tick_size=0.01,
            min_notional=5.0,
            quantity_precision=3,
            price_precision=2,
        )
    }
    executor = BinanceFuturesOrderExecutor(
        api_key="k", api_secret="s", symbol_filters=filters
    )
    await executor.connect()

    captured = {}

    def callback(url, **kwargs):
        captured["url"] = str(url)

    with aioresponses() as m:
        m.post(
            re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v1/order") + r".*"),
            payload={
                "orderId": 1,
                "status": "FILLED",
                "executedQty": "1.234",
                "avgPrice": "100.0",
            },
            callback=callback,
        )
        await executor.submit_order(_order_request(quantity=1.23456789))

    query = parse_qs(urlparse(captured["url"]).query)
    assert query["quantity"][0] == "1.234"

    await executor.close()


@pytest.mark.asyncio
async def test_submit_order_rejects_below_min_notional_without_hitting_api():
    from aitos.exchange.symbol_filters import SymbolFilters

    filters = {
        "BTCUSDT": SymbolFilters(
            symbol="BTCUSDT",
            step_size=0.001,
            tick_size=0.01,
            min_notional=100.0,
            quantity_precision=3,
            price_precision=2,
        )
    }
    executor = BinanceFuturesOrderExecutor(
        api_key="k", api_secret="s", symbol_filters=filters
    )
    await executor.connect()

    with aioresponses() as m:  # no mock registered — a network call would raise ConnectionError
        result = await executor.submit_order(
            _order_request(quantity=0.0001)
        )  # notional ~= 0.01, well below 100

    assert result.success is False
    assert (
        "min notional" in result.error.lower()
        or "minimum notional" in result.error.lower()
    )

    await executor.close()


@pytest.mark.asyncio
async def test_load_symbol_filters_updates_precision_after_construction():
    from aitos.exchange.symbol_filters import SymbolFilters

    executor = BinanceFuturesOrderExecutor(api_key="k", api_secret="s")
    await executor.connect()
    executor.load_symbol_filters(
        {
            "BTCUSDT": SymbolFilters(
                symbol="BTCUSDT",
                step_size=0.01,
                tick_size=0.1,
                min_notional=1.0,
                quantity_precision=2,
                price_precision=1,
            )
        }
    )

    captured = {}

    def callback(url, **kwargs):
        captured["url"] = str(url)

    with aioresponses() as m:
        m.post(
            re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v1/order") + r".*"),
            payload={
                "orderId": 1,
                "status": "FILLED",
                "executedQty": "1.23",
                "avgPrice": "100.0",
            },
            callback=callback,
        )
        await executor.submit_order(_order_request(quantity=1.239))

    query = parse_qs(urlparse(captured["url"]).query)
    assert query["quantity"][0] == "1.23"

    await executor.close()


@pytest.mark.asyncio
async def test_get_order_status_and_cancel_order():
    executor = BinanceFuturesOrderExecutor(api_key="k", api_secret="s")
    await executor.connect()

    with aioresponses() as m:
        m.get(
            re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v1/order") + r".*"),
            payload={"orderId": 1, "status": "FILLED"},
        )
        status = await executor.get_order_status("BTCUSDT", "1")
    assert status["status"] == "FILLED"

    with aioresponses() as m:
        m.delete(
            re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v1/order") + r".*"),
            payload={"orderId": 1, "status": "CANCELED"},
        )
        cancel_result = await executor.cancel_order("BTCUSDT", "1")
    assert cancel_result["status"] == "CANCELED"

    await executor.close()


@pytest.mark.asyncio
async def test_set_leverage():
    executor = BinanceFuturesOrderExecutor(api_key="k", api_secret="s")
    await executor.connect()

    with aioresponses() as m:
        m.post(
            re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v1/leverage") + r".*"),
            payload={"leverage": 10, "symbol": "BTCUSDT"},
        )
        result = await executor.set_leverage("BTCUSDT", 10)
    assert result["leverage"] == 10

    await executor.close()


@pytest.mark.asyncio
async def test_place_stop_loss_order_uses_reduce_only_and_opposite_side():
    executor = BinanceFuturesOrderExecutor(api_key="k", api_secret="s")
    await executor.connect()

    captured = {}

    def callback(url, **kwargs):
        captured["url"] = str(url)

    with aioresponses() as m:
        m.post(
            re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v1/order") + r".*"),
            payload={"orderId": 999, "status": "NEW"},
            callback=callback,
        )
        result = await executor.place_stop_loss_order(
            "BTCUSDT", TradeSide.LONG, 1.5, 98.0
        )

    assert result.success is True
    assert result.order_id == "999"
    query = parse_qs(urlparse(captured["url"]).query)
    assert query["side"][0] == "SELL"  # closing a LONG requires a SELL
    assert query["type"][0] == "STOP_MARKET"
    assert query["reduceOnly"][0] == "true"
    assert query["stopPrice"][0] == "98.0"

    await executor.close()


@pytest.mark.asyncio
async def test_place_take_profit_order_for_short_uses_buy_side():
    executor = BinanceFuturesOrderExecutor(api_key="k", api_secret="s")
    await executor.connect()

    captured = {}

    def callback(url, **kwargs):
        captured["url"] = str(url)

    with aioresponses() as m:
        m.post(
            re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v1/order") + r".*"),
            payload={"orderId": 1000, "status": "NEW"},
            callback=callback,
        )
        result = await executor.place_take_profit_order(
            "BTCUSDT", TradeSide.SHORT, 2.0, 90.0
        )

    assert result.success is True
    query = parse_qs(urlparse(captured["url"]).query)
    assert query["side"][0] == "BUY"  # closing a SHORT requires a BUY
    assert query["type"][0] == "TAKE_PROFIT_MARKET"

    await executor.close()


@pytest.mark.asyncio
async def test_place_stop_loss_order_returns_failure_on_api_error():
    executor = BinanceFuturesOrderExecutor(api_key="k", api_secret="s")
    await executor.connect()

    with aioresponses() as m:
        m.post(
            re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v1/order") + r".*"),
            status=400,
            payload={"code": -2021, "msg": "Order would immediately trigger"},
        )
        result = await executor.place_stop_loss_order(
            "BTCUSDT", TradeSide.LONG, 1.0, 98.0
        )

    assert result.success is False
    assert "-2021" in result.error

    await executor.close()


@pytest.mark.asyncio
async def test_cancel_resting_order_swallows_api_errors():
    executor = BinanceFuturesOrderExecutor(api_key="k", api_secret="s")
    await executor.connect()

    with aioresponses() as m:
        m.delete(
            re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v1/order") + r".*"),
            status=400,
            payload={"code": -2011, "msg": "Unknown order sent"},
        )
        await executor.cancel_resting_order("BTCUSDT", "123")  # must not raise

    await executor.close()


@pytest.mark.asyncio
async def test_get_resting_order_status_returns_status_string():
    executor = BinanceFuturesOrderExecutor(api_key="k", api_secret="s")
    await executor.connect()

    with aioresponses() as m:
        m.get(
            re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v1/order") + r".*"),
            payload={"orderId": 1, "status": "FILLED"},
        )
        status = await executor.get_resting_order_status("BTCUSDT", "1")

    assert status == "FILLED"
    await executor.close()


@pytest.mark.asyncio
async def test_hedge_mode_open_long_sends_buy_and_position_side_long():
    executor = BinanceFuturesOrderExecutor(api_key="k", api_secret="s", hedge_mode=True)
    assert executor.hedge_mode is True
    await executor.connect()

    captured = {}

    def callback(url, **kwargs):
        captured["url"] = str(url)

    with aioresponses() as m:
        m.post(
            re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v1/order") + r".*"),
            payload={
                "orderId": 1,
                "status": "FILLED",
                "executedQty": "1.0",
                "avgPrice": "100.0",
            },
            callback=callback,
        )
        await executor.submit_order(_order_request(side=TradeSide.LONG))

    query = parse_qs(urlparse(captured["url"]).query)
    assert query["side"][0] == "BUY"
    assert query["positionSide"][0] == "LONG"
    assert (
        "reduceOnly" not in query
    )  # Binance rejects reduceOnly + positionSide together

    await executor.close()


@pytest.mark.asyncio
async def test_hedge_mode_open_short_sends_sell_and_position_side_short():
    executor = BinanceFuturesOrderExecutor(api_key="k", api_secret="s", hedge_mode=True)
    await executor.connect()

    captured = {}

    def callback(url, **kwargs):
        captured["url"] = str(url)

    with aioresponses() as m:
        m.post(
            re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v1/order") + r".*"),
            payload={
                "orderId": 1,
                "status": "FILLED",
                "executedQty": "1.0",
                "avgPrice": "100.0",
            },
            callback=callback,
        )
        await executor.submit_order(_order_request(side=TradeSide.SHORT))

    query = parse_qs(urlparse(captured["url"]).query)
    assert query["side"][0] == "SELL"
    assert query["positionSide"][0] == "SHORT"

    await executor.close()


@pytest.mark.asyncio
async def test_hedge_mode_closing_long_sends_sell_but_keeps_position_side_long():
    """Closing a LONG in hedge mode is a SELL order, but positionSide stays
    LONG (it identifies *which* position this affects, not the order direction)."""
    executor = BinanceFuturesOrderExecutor(api_key="k", api_secret="s", hedge_mode=True)
    await executor.connect()

    captured = {}

    def callback(url, **kwargs):
        captured["url"] = str(url)

    with aioresponses() as m:
        m.post(
            re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v1/order") + r".*"),
            payload={"orderId": 1, "status": "NEW"},
            callback=callback,
        )
        await executor.place_stop_loss_order("BTCUSDT", TradeSide.LONG, 1.0, 98.0)

    query = parse_qs(urlparse(captured["url"]).query)
    assert query["side"][0] == "SELL"
    assert query["positionSide"][0] == "LONG"
    assert "reduceOnly" not in query

    await executor.close()


@pytest.mark.asyncio
async def test_one_way_mode_still_sends_reduce_only_and_no_position_side():
    """Regression check: the default (one-way) mode's request shape is
    unchanged by the hedge-mode refactor."""
    executor = BinanceFuturesOrderExecutor(
        api_key="k", api_secret="s"
    )  # hedge_mode defaults to False
    assert executor.hedge_mode is False
    await executor.connect()

    captured = {}

    def callback(url, **kwargs):
        captured["url"] = str(url)

    with aioresponses() as m:
        m.post(
            re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v1/order") + r".*"),
            payload={"orderId": 1, "status": "NEW"},
            callback=callback,
        )
        await executor.place_stop_loss_order("BTCUSDT", TradeSide.LONG, 1.0, 98.0)

    query = parse_qs(urlparse(captured["url"]).query)
    assert query["side"][0] == "SELL"
    assert query["reduceOnly"][0] == "true"
    assert "positionSide" not in query

    await executor.close()


@pytest.mark.asyncio
async def test_get_position_mode_reflects_account_setting():
    executor = BinanceFuturesOrderExecutor(api_key="k", api_secret="s")
    await executor.connect()

    with aioresponses() as m:
        m.get(
            re.compile(
                r"^" + re.escape(TESTNET_URL + "/fapi/v1/positionSide/dual") + r".*"
            ),
            payload={"dualSidePosition": True},
        )
        is_hedge = await executor.get_position_mode()

    assert is_hedge is True
    await executor.close()


@pytest.mark.asyncio
async def test_set_position_mode_updates_account_and_local_flag():
    executor = BinanceFuturesOrderExecutor(api_key="k", api_secret="s")
    assert executor.hedge_mode is False
    await executor.connect()

    captured = {}

    def callback(url, **kwargs):
        captured["url"] = str(url)

    with aioresponses() as m:
        m.post(
            re.compile(
                r"^" + re.escape(TESTNET_URL + "/fapi/v1/positionSide/dual") + r".*"
            ),
            payload={"code": 200, "msg": "success"},
            callback=callback,
        )
        await executor.set_position_mode(True)

    assert executor.hedge_mode is True
    query = parse_qs(urlparse(captured["url"]).query)
    assert query["dualSidePosition"][0] == "true"

    await executor.close()


@pytest.mark.asyncio
async def test_get_account_balance_returns_matching_asset():
    executor = BinanceFuturesOrderExecutor(api_key="k", api_secret="s")
    await executor.connect()

    with aioresponses() as m:
        m.get(
            re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v2/balance") + r".*"),
            payload=[
                {"asset": "USDT", "balance": "1000.0", "availableBalance": "950.5"},
                {"asset": "BUSD", "balance": "0.0", "availableBalance": "0.0"},
            ],
        )
        balance = await executor.get_account_balance("USDT")

    assert balance == 950.5
    await executor.close()


@pytest.mark.asyncio
async def test_get_account_balance_returns_zero_for_missing_asset():
    executor = BinanceFuturesOrderExecutor(api_key="k", api_secret="s")
    await executor.connect()

    with aioresponses() as m:
        m.get(
            re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v2/balance") + r".*"),
            payload=[{"asset": "BUSD", "balance": "0.0", "availableBalance": "0.0"}],
        )
        balance = await executor.get_account_balance("USDT")

    assert balance == 0.0
    await executor.close()


def _order_request(side=TradeSide.LONG, quantity=1.0):
    from aitos.execution.order_executor import OrderRequest

    return OrderRequest(
        symbol="BTCUSDT", side=side, quantity=quantity, reference_price=100.0
    )
