"""Offline venue-response contracts: ACK is not fill and uncertainty is not rejection."""

from unittest.mock import AsyncMock

import pytest

from aitos.execution.binance_executor import (
    BinanceAPIError,
    BinanceFuturesOrderExecutor,
)
from aitos.execution.order_executor import OrderRequest
from aitos.models.trade import TradeSide


def request(**kw):
    values = dict(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        reference_price=100.0,
        client_order_id="logical-entry",
    )
    values.update(kw)
    return OrderRequest(**values)


def executor(*responses):
    value = BinanceFuturesOrderExecutor("dummy", "dummy")
    value._signed_request = AsyncMock(side_effect=list(responses))
    return value


def fill(**kw):
    values = dict(orderId=12, status="FILLED", executedQty="1", avgPrice="101")
    values.update(kw)
    return values


@pytest.mark.parametrize("status", ["NEW", "PARTIALLY_FILLED", "UNKNOWN"])
async def test_unconfirmed_order_is_never_reported_as_full_fill(status):
    response = fill(status=status, executedQty="0", avgPrice="0")
    value = executor(response, response, response, response)
    with pytest.raises(RuntimeError, match="uncertain"):
        await value.submit_order(request())


@pytest.mark.parametrize(
    "field,bad",
    [
        ("executedQty", "0"),
        ("executedQty", "nan"),
        ("executedQty", "inf"),
        ("executedQty", "-1"),
        ("executedQty", "2"),
        ("avgPrice", "0"),
        ("avgPrice", "nan"),
        ("avgPrice", "inf"),
    ],
)
async def test_malformed_filled_response_never_creates_a_position(field, bad):
    value = executor(fill(**{field: bad}))
    with pytest.raises(RuntimeError, match="uncertain"):
        await value.submit_order(request())


async def test_ack_then_authoritative_fill_uses_venue_economics():
    value = executor(fill(status="NEW", executedQty="0", avgPrice="0"), fill())
    result = await value.submit_order(request())
    assert result.filled_quantity == 1
    assert result.fill_price == 101
    assert value._signed_request.call_args_list[1].args[0] == "GET"
    assert (
        value._signed_request.call_args_list[0].args[2]["newOrderRespType"] == "RESULT"
    )


async def test_terminal_cancel_with_partial_fill_is_actual_exposure_not_rejection():
    value = executor(fill(status="CANCELED", executedQty="0.25"))
    result = await value.submit_order(request())
    assert result.success
    assert result.filled_quantity == 0.25


async def test_terminal_cancel_without_fill_is_not_success():
    value = executor(fill(status="CANCELED", executedQty="0", avgPrice="0"))
    result = await value.submit_order(request())
    assert not result.success
    assert result.filled_quantity == 0


async def test_ambiguous_exchange_error_is_not_a_definitive_rejection():
    value = executor(BinanceAPIError("Binance API error -1007: Timeout"), None)
    with pytest.raises(RuntimeError, match="uncertain"):
        await value.submit_order(request())


async def test_timeout_reconciles_by_client_identity_without_second_post():
    value = executor(TimeoutError("offline fault"), fill())
    result = await value.submit_order(request())
    assert result.success
    calls = value._signed_request.call_args_list
    assert [c.args[0] for c in calls] == ["POST", "GET"]
    assert calls[1].args[2]["origClientOrderId"] == "logical-entry"


async def test_bounded_cancel_then_terminal_partial_fill():
    value = executor(
        fill(status="NEW", executedQty="0", avgPrice="0"),
        fill(status="PARTIALLY_FILLED", executedQty="0.25"),
        fill(status="CANCELED", executedQty="0.25"),
        fill(status="CANCELED", executedQty="0.25"),
    )
    result = await value.submit_order(request())
    assert result.success and result.filled_quantity == 0.25
    assert [call.args[0] for call in value._signed_request.call_args_list] == [
        "POST",
        "GET",
        "DELETE",
        "GET",
    ]


async def test_cancel_timeout_keeps_exposure_uncertain():
    value = executor(
        fill(status="NEW", executedQty="0"),
        fill(status="NEW", executedQty="0"),
        TimeoutError("cancel fault"),
    )
    with pytest.raises(RuntimeError, match="uncertain"):
        await value.submit_order(request())
    assert value.has_unresolved_orders("BTCUSDT")


async def test_not_found_after_submit_timeout_is_not_rejection():
    value = executor(
        TimeoutError("submit fault"), BinanceAPIError("Binance API error -2013: absent")
    )
    with pytest.raises(RuntimeError, match="uncertain"):
        await value.submit_order(request())


async def test_cumulative_quote_can_establish_price_but_reference_price_cannot():
    value = executor(fill(avgPrice="0", cumQuote="102"))
    assert (await value.submit_order(request())).fill_price == 102


@pytest.mark.parametrize(
    "field,bad",
    [("symbol", "ETHUSDT"), ("side", "SELL"), ("clientOrderId", "other-intent")],
)
async def test_unrelated_order_evidence_is_not_accepted(field, bad):
    value = executor(fill(**{field: bad}))
    with pytest.raises(RuntimeError, match="uncertain"):
        await value.submit_order(request())


@pytest.mark.parametrize("field", ["quantity", "reference_price"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0, True])
async def test_direct_executor_invalid_economics_fail_before_http(field, bad):
    value = executor()
    with pytest.raises(ValueError):
        await value.submit_order(request(**{field: bad}))
    value._signed_request.assert_not_called()
