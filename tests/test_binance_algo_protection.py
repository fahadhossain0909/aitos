"""Current official USD-M conditional-order schema; offline HTTP boundary."""

import json
from unittest.mock import AsyncMock

import pytest

from aitos.execution.binance_executor import (
    BinanceAPIError,
    BinanceFuturesOrderExecutor,
)
from aitos.execution.intent_store import ExecutionIntentStore, ExecutionUncertainError
from aitos.models.trade import TradeSide


@pytest.mark.parametrize("hedge", [False, True])
@pytest.mark.parametrize("kind", ["sl", "tp"])
async def test_conditional_protection_uses_algo_endpoint_and_namespace(hedge, kind):
    value = BinanceFuturesOrderExecutor("dummy", "dummy", hedge_mode=hedge)
    value._signed_request = AsyncMock(return_value=dict(algoId=321, algoStatus="NEW"))
    if kind == "sl":
        result = await value.place_stop_loss_order("BTCUSDT", TradeSide.LONG, 1, 98)
    else:
        result = await value.place_take_profit_order("BTCUSDT", TradeSide.LONG, 1, 106)
    assert result.success and result.order_id == "algo:321"
    assert result.filled_quantity == 0
    method, path, params = value._signed_request.call_args.args
    assert (method, path) == ("POST", "/fapi/v1/algoOrder")
    assert params["algoType"] == "CONDITIONAL"
    assert "triggerPrice" in params and "stopPrice" not in params
    assert "clientAlgoId" in params and "newClientOrderId" not in params
    assert params["side"] == "SELL"
    if hedge:
        assert params["positionSide"] == "LONG" and "reduceOnly" not in params
    else:
        assert params["reduceOnly"] == "true"


@pytest.mark.parametrize("status", ["TRIGGERED", "FINISHED"])
async def test_triggered_algo_needs_actual_child_fill(status):
    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    value._signed_request = AsyncMock(
        side_effect=[
            dict(algoId=321, symbol="BTCUSDT", algoStatus=status, actualOrderId="123"),
            dict(orderId=123, status="FILLED", executedQty="1", avgPrice="97.5"),
        ]
    )
    evidence = await value.get_order_status("BTCUSDT", "algo:321")
    assert evidence["executedQty"] == "1" and evidence["avgPrice"] == "97.5"
    assert evidence["sourceAlgoId"] == "algo:321"
    assert value._signed_request.call_args_list[1].args[2]["orderId"] == "123"


@pytest.mark.parametrize("status", ["TRIGGERED", "FINISHED"])
async def test_triggered_without_child_is_uncertain_not_filled(status):
    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    value._signed_request = AsyncMock(
        return_value=dict(algoId=321, algoStatus=status, actualOrderId="")
    )
    with pytest.raises(ExecutionUncertainError):
        await value.get_resting_order_status("BTCUSDT", "algo:321")


async def test_child_new_is_not_filled_even_when_algo_finished():
    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    value._signed_request = AsyncMock(
        side_effect=[
            dict(algoId=321, algoStatus="FINISHED", actualOrderId="123"),
            dict(orderId=123, status="NEW", executedQty="0", avgPrice="0"),
        ]
    )
    assert await value.get_resting_order_status("BTCUSDT", "algo:321") == "NEW"


async def test_algo_cancel_requires_followup_terminal_query():
    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    value._signed_request = AsyncMock(
        side_effect=[
            dict(algoId=321, code="200", msg="success"),
            dict(algoId=321, algoStatus="CANCELED"),
        ]
    )
    await value.cancel_resting_order("BTCUSDT", "algo:321")
    calls = value._signed_request.call_args_list
    assert [call.args[0] for call in calls] == ["DELETE", "GET"]
    assert calls[0].args[1] == "/fapi/v1/algoOrder"


async def test_wrong_algo_identity_is_rejected():
    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    value._signed_request = AsyncMock(return_value=dict(algoId=999, algoStatus="NEW"))
    with pytest.raises(ExecutionUncertainError):
        await value.get_order_status("BTCUSDT", "algo:321")


async def test_legacy_persisted_stop_ids_still_use_classic_query():
    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    value._signed_request = AsyncMock(return_value=dict(orderId=123, status="NEW"))
    assert await value.get_resting_order_status("BTCUSDT", "123") == "NEW"
    assert value._signed_request.call_args.args[1] == "/fapi/v1/order"


async def test_terminal_partial_child_is_reported_for_actual_exposure_reconciliation():
    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    value._signed_request = AsyncMock(
        side_effect=[
            dict(algoId=321, algoStatus="FINISHED", actualOrderId="123"),
            dict(orderId=123, status="CANCELED", executedQty=".25", avgPrice="97.5"),
        ]
    )
    assert (
        await value.get_resting_order_status("BTCUSDT", "algo:321")
        == "PARTIALLY_FILLED_TERMINAL"
    )


async def test_open_algo_inventory_is_read_only_and_validates_complete_rows():
    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    row = dict(
        algoId=321,
        clientAlgoId="aitos-stop-1",
        algoType="CONDITIONAL",
        orderType="STOP_MARKET",
        symbol="BTCUSDT",
        side="SELL",
        positionSide="BOTH",
        quantity="1.0",
        triggerPrice="98.5",
        algoStatus="NEW",
        closePosition=False,
        reduceOnly=False,
    )
    value._signed_request = AsyncMock(return_value=[row])

    result = await value.get_open_algo_orders("btcusdt")

    assert result == [row] and result[0] is not row
    assert value._signed_request.call_args.args == (
        "GET",
        "/fapi/v1/openAlgoOrders",
        {"algoType": "CONDITIONAL", "symbol": "BTCUSDT"},
    )


@pytest.mark.parametrize(
    "override",
    [
        {"algoId": 0},
        {"clientAlgoId": ""},
        {"symbol": "ETHUSDT"},
        {"algoStatus": "TRIGGERED"},
        {"orderType": "LIMIT"},
        {"side": "HOLD"},
        {"positionSide": "UNKNOWN"},
        {"quantity": "NaN"},
        {"triggerPrice": "0"},
        {"reduceOnly": 1},
    ],
)
async def test_open_algo_inventory_rejects_malformed_or_mismatched_rows(override):
    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    row = dict(
        algoId=321,
        clientAlgoId="aitos-stop-1",
        algoType="CONDITIONAL",
        orderType="STOP_MARKET",
        symbol="BTCUSDT",
        side="SELL",
        positionSide="BOTH",
        quantity="1.0",
        triggerPrice="98.5",
        algoStatus="NEW",
        closePosition=False,
        reduceOnly=False,
    )
    row.update(override)
    value._signed_request = AsyncMock(return_value=[row])

    with pytest.raises(ValueError):
        await value.get_open_algo_orders("BTCUSDT")


async def test_open_algo_inventory_rejects_duplicate_identities():
    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    row = dict(
        algoId=321,
        clientAlgoId="aitos-stop-1",
        algoType="CONDITIONAL",
        orderType="STOP_MARKET",
        symbol="BTCUSDT",
        side="SELL",
        positionSide="BOTH",
        quantity="1.0",
        triggerPrice="98.5",
        algoStatus="NEW",
        closePosition=False,
        reduceOnly=False,
    )
    value._signed_request = AsyncMock(return_value=[row, dict(row)])

    with pytest.raises(ValueError, match="duplicate"):
        await value.get_open_algo_orders()


async def test_open_order_inventory_is_read_only_and_symbol_scoped():
    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    row = dict(
        orderId=456,
        symbol="BTCUSDT",
        status="PARTIALLY_FILLED",
        side="BUY",
        positionSide="BOTH",
        type="LIMIT",
        origQty="2.0",
        executedQty="0.5",
        reduceOnly=False,
        closePosition=False,
    )
    value._signed_request = AsyncMock(return_value=[row])

    result = await value.get_open_orders("btcusdt")

    assert result == [row] and result[0] is not row
    assert value._signed_request.call_args.args == (
        "GET",
        "/fapi/v1/openOrders",
        {"symbol": "BTCUSDT"},
    )


@pytest.mark.parametrize(
    "override",
    [
        {"orderId": 0},
        {"symbol": "ETHUSDT"},
        {"status": "FILLED"},
        {"side": "UNKNOWN"},
        {"positionSide": "UNKNOWN"},
        {"type": "UNKNOWN"},
        {"origQty": "NaN"},
        {"executedQty": "3"},
        {"reduceOnly": "unknown"},
        {"closePosition": None},
        {"reduceOnly": 1},
        {"closePosition": 0},
    ],
)
async def test_open_order_inventory_rejects_invalid_rows(override):
    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    row = dict(
        orderId=456,
        symbol="BTCUSDT",
        status="NEW",
        side="BUY",
        positionSide="BOTH",
        type="LIMIT",
        origQty="2.0",
        executedQty="0.0",
        reduceOnly=False,
        closePosition=False,
    )
    row.update(override)
    value._signed_request = AsyncMock(return_value=[row])

    with pytest.raises(ValueError):
        await value.get_open_orders("BTCUSDT")


async def test_algo_client_id_lookup_recovers_accepted_protection_identity():
    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    response = dict(
        algoId=321,
        clientAlgoId="aitos-stop-1",
        algoType="CONDITIONAL",
        orderType="STOP_MARKET",
        symbol="BTCUSDT",
        side="SELL",
        positionSide="BOTH",
        algoStatus="NEW",
    )
    value._signed_request = AsyncMock(return_value=response)

    result = await value.get_algo_order_by_client_id("btcusdt", "aitos-stop-1")

    assert result == response and result is not response
    assert value._signed_request.call_args.args == (
        "GET",
        "/fapi/v1/algoOrder",
        {"clientAlgoId": "aitos-stop-1"},
    )


async def test_algo_client_id_lookup_returns_none_only_for_explicit_not_found():
    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    value._signed_request = AsyncMock(
        side_effect=BinanceAPIError("Binance API error -2013: not found", code=-2013)
    )

    assert await value.get_algo_order_by_client_id("BTCUSDT", "aitos-stop-1") is None

    value._signed_request = AsyncMock(
        side_effect=BinanceAPIError("Binance API error -1000: unknown", code=-1000)
    )
    with pytest.raises(BinanceAPIError):
        await value.get_algo_order_by_client_id("BTCUSDT", "aitos-stop-1")


async def test_algo_client_id_lookup_rejects_cross_symbol_or_client_identity():
    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    value._signed_request = AsyncMock(
        return_value=dict(
            algoId=321,
            clientAlgoId="different-client",
            algoType="CONDITIONAL",
            orderType="STOP_MARKET",
            symbol="ETHUSDT",
            side="SELL",
            positionSide="BOTH",
            algoStatus="NEW",
        )
    )

    with pytest.raises(ExecutionUncertainError):
        await value.get_algo_order_by_client_id("BTCUSDT", "aitos-stop-1")


async def test_protective_preclaim_persists_owner_trade_identity(tmp_path):
    store = ExecutionIntentStore(tmp_path / "protective.sqlite", scope="test-account")
    value = BinanceFuturesOrderExecutor("dummy", "dummy", intent_store=store)
    value._signed_request = AsyncMock(return_value=dict(algoId=321, algoStatus="NEW"))

    result = await value.place_stop_loss_order(
        "BTCUSDT",
        TradeSide.LONG,
        1.0,
        98.0,
        owner_trade_id="trade-owner-1",
    )

    assert result.success is True
    row = store.protection_intents()[0]
    request = json.loads(row["request_json"])
    assert request["owner_trade_id"] == "trade-owner-1"
    assert row["state"] == "PROTECTED"
