"""Current official USD-M conditional-order schema; offline HTTP boundary."""

from unittest.mock import AsyncMock

import pytest

from aitos.execution.binance_executor import BinanceFuturesOrderExecutor
from aitos.execution.intent_store import ExecutionUncertainError
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
