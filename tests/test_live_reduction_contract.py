"""Lifecycle must reduce actual live exposure before local close accounting."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from aitos.execution.intent_store import ExecutionUncertainError
from aitos.execution.order_executor import OrderResult, SimulatedOrderExecutor
from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from aitos.trading.lifecycle import TradeLifecycle


class Venue(SimulatedOrderExecutor):
    requires_exchange_execution = True

    def __init__(self, *, fraction=1, success=True):
        super().__init__()
        self.requests = []
        self.fraction = fraction
        self.success = success
        self.acknowledged = []

    async def submit_order(self, request):
        self.requests.append(request)
        await asyncio.sleep(0)
        return OrderResult(
            "venue-exit",
            request.symbol,
            request.side,
            request.quantity * self.fraction if self.success else 0,
            110 if self.success else 0,
            success=self.success,
            error="isolated rejection",
        )

    def acknowledge_execution(self, client_order_id, trade_id):
        self.acknowledged.append((client_order_id, trade_id))


def trade():
    return Trade(
        trade_id="live-one",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100,
        quantity=2,
        leverage=1,
        position_size_usd=200,
        risk_amount_usd=4,
        strategy_id="isolated",
        agent_consensus={},
        explanation="regression",
        sl_price=98,
        tp_price=120,
        take_profit_levels=[120],
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time="2026-11-07T00:00:00+00:00",
    )


async def lifecycle(event_bus, risk_engine, venue):
    value = TradeLifecycle(event_bus, risk_engine, venue)
    await value.initialize({})
    value._execution_position_verifier = AsyncMock()
    value._execution_state_writer = AsyncMock()
    position = trade()
    value._open_trades[position.trade_id] = position
    return value, position


async def test_live_close_submits_reduce_only_and_accounts_actual_fill(
    event_bus, risk_engine
):
    venue = Venue()
    value, position = await lifecycle(event_bus, risk_engine, venue)
    closed = await value.close_trade(position.trade_id, 105, "isolated close")
    assert len(venue.requests) == 1
    request = venue.requests[0]
    assert request.reduce_only and request.side == TradeSide.LONG
    assert request.quantity == 2
    assert request.client_order_id and len(request.client_order_id) <= 36
    assert closed.exit_price == 110
    assert closed.pnl == 20
    assert venue.acknowledged
    value._execution_state_writer.assert_awaited()


async def test_live_close_rejection_keeps_local_position_open(event_bus, risk_engine):
    venue = Venue(success=False)
    value, position = await lifecycle(event_bus, risk_engine, venue)
    with pytest.raises(RuntimeError, match="reduction"):
        await value.close_trade(position.trade_id, 105, "isolated close")
    assert value.get_open_trades() == [position]
    assert position.quantity == 2
    assert position.pnl is None
    assert not venue.acknowledged


async def test_live_partial_uses_actual_quantity_and_price(event_bus, risk_engine):
    venue = Venue(fraction=0.25)
    value, position = await lifecycle(event_bus, risk_engine, venue)
    await value._partial_close(position, 105, 0.5)
    assert venue.requests[0].quantity == 1
    assert position.quantity == 1.75
    assert position.position_size_usd == 175
    assert position.risk_amount_usd == 3.5
    assert position.gross_realized_pnl_usd == 2.5
    assert position.partial_exits[0].price == 110
    assert venue.acknowledged


async def test_partial_execution_of_full_exit_does_not_mark_position_closed(
    event_bus, risk_engine
):
    venue = Venue(fraction=0.25)
    value, position = await lifecycle(event_bus, risk_engine, venue)
    remaining = await value.close_trade(position.trade_id, 105, "isolated close")
    assert remaining.state == TradeLifecycleState.POSITION_OPENED
    assert remaining.quantity == 1.5
    assert remaining.gross_realized_pnl_usd == 5
    assert value.get_open_trades() == [remaining]


async def test_concurrent_live_closes_cannot_submit_two_reductions(
    event_bus, risk_engine
):
    venue = Venue()
    value, position = await lifecycle(event_bus, risk_engine, venue)
    results = await asyncio.gather(
        *[
            value.close_trade(position.trade_id, 105, "isolated close")
            for _ in range(2)
        ],
        return_exceptions=True
    )
    assert len(venue.requests) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1


async def test_missing_durable_writer_blocks_before_venue_side_effect(
    event_bus, risk_engine
):
    venue = Venue()
    value, position = await lifecycle(event_bus, risk_engine, venue)
    del value._execution_state_writer
    with pytest.raises(RuntimeError, match="durable"):
        await value.close_trade(position.trade_id, 105, "isolated close")
    assert not venue.requests


async def test_apply_failure_latches_in_process_recovery_not_second_exit(
    event_bus, risk_engine
):
    venue = Venue()
    value, position = await lifecycle(event_bus, risk_engine, venue)
    value._execution_state_writer.side_effect = OSError("isolated persistence failure")
    with pytest.raises((OSError, ExecutionUncertainError)):
        await value.close_trade(position.trade_id, 105, "isolated close")
    with pytest.raises(ExecutionUncertainError):
        await value.close_trade(position.trade_id, 105, "retry")
    assert len(venue.requests) == 1
    assert not venue.acknowledged


async def test_concurrent_live_price_updates_consume_checkpoint_only_once(
    event_bus, risk_engine
):
    venue = Venue()
    value, position = await lifecycle(event_bus, risk_engine, venue)
    position.take_profit_levels = [105, 120]
    await asyncio.gather(
        value.update_price(position.trade_id, 106),
        value.update_price(position.trade_id, 106),
    )
    assert len(venue.requests) == 1
    assert position.quantity == 1
    assert position.take_profit_levels == [120]


async def test_failed_position_verification_never_acknowledges_reduction(
    event_bus, risk_engine
):
    venue = Venue()
    value, position = await lifecycle(event_bus, risk_engine, venue)
    value._execution_position_verifier.side_effect = ValueError(
        "account exposure mismatch"
    )
    with pytest.raises(ValueError, match="mismatch"):
        await value.close_trade(position.trade_id, 105, "isolated close")
    assert not venue.acknowledged
    with pytest.raises(ExecutionUncertainError):
        await value.close_trade(position.trade_id, 105, "retry")
    assert len(venue.requests) == 1


async def test_full_fraction_reduction_uses_full_close_transition(
    event_bus, risk_engine
):
    venue = Venue()
    value, position = await lifecycle(event_bus, risk_engine, venue)
    await value._partial_close(position, 105, 1)
    assert position.state == TradeLifecycleState.POSITION_CLOSED
    assert not value.get_open_trades()
    assert len(venue.requests) == 1


async def test_terminal_partial_stop_reconciles_without_second_market_reduction(
    event_bus, risk_engine
):
    venue = Venue()
    venue.get_resting_order_status = AsyncMock(return_value="PARTIALLY_FILLED_TERMINAL")
    venue.get_order_status = AsyncMock(
        return_value=dict(status="CANCELED", executedQty="1", avgPrice="97.5")
    )
    venue.place_stop_loss_order = AsyncMock(
        return_value=OrderResult("replacement-sl", "BTCUSDT", TradeSide.LONG, 0, 98)
    )
    venue.cancel_resting_order = AsyncMock()
    value, position = await lifecycle(event_bus, risk_engine, venue)
    value._use_exchange_side_stops = True
    position.sl_order_id = "algo:12"
    result = await value.reconcile_trade(position.trade_id)
    assert result.quantity == 1
    assert result.state == TradeLifecycleState.POSITION_OPENED
    assert result.take_profit_levels == [120]
    assert result.sl_order_id == "replacement-sl"
    assert not venue.requests


async def test_partial_exit_is_not_reported_as_completed_close(
    event_bus, risk_engine, monkeypatch
):
    import aitos.intelligence.position_runtime as telemetry

    observed = []
    monkeypatch.setattr(
        telemetry,
        "emit_position_event",
        lambda name, *args, **kwargs: observed.append(name),
    )
    value, position = await lifecycle(event_bus, risk_engine, Venue(fraction=0.25))
    await value.close_trade(position.trade_id, 105, "partial terminal fill")
    assert "position.close.partial" in observed
    assert "position.close.completed" not in observed


async def test_failed_live_stop_replacement_never_cancels_existing_protection(
    event_bus, risk_engine
):
    venue = Venue()
    venue.place_stop_loss_order = AsyncMock(
        return_value=OrderResult(
            "rejected", "BTCUSDT", TradeSide.LONG, 0, 0, success=False
        )
    )
    venue.cancel_resting_order = AsyncMock()
    value, position = await lifecycle(event_bus, risk_engine, venue)
    position.sl_order_id = "original-protective-stop"
    with pytest.raises(ExecutionUncertainError):
        await value._replace_exchange_side_stop_loss(position)
    assert position.sl_order_id == "original-protective-stop"
    venue.cancel_resting_order.assert_not_called()


async def test_failed_old_stop_cancellation_keeps_durable_recovery_identity(
    event_bus, risk_engine
):
    venue = Venue()
    venue.place_stop_loss_order = AsyncMock(
        return_value=OrderResult("replacement", "BTCUSDT", TradeSide.LONG, 0, 98)
    )
    venue.cancel_resting_order = AsyncMock(side_effect=OSError("isolated cancel fault"))
    value, position = await lifecycle(event_bus, risk_engine, venue)
    position.sl_order_id = "original-protective-stop"
    with pytest.raises(OSError):
        await value._replace_exchange_side_stop_loss(position)
    assert position.sl_order_id == "replacement"
    assert position.agent_consensus["_pending_stop_cancellations"] == [
        "original-protective-stop"
    ]
    value._execution_state_writer.assert_awaited()
    assert value._execution_recovery_required
