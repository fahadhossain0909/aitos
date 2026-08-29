import pytest

from aitos.execution.order_executor import (
    OrderExecutor,
    OrderRequest,
    OrderResult,
    SimulatedOrderExecutor,
)
from aitos.models.trade import Opportunity, TradeLifecycleState, TradeSide
from aitos.risk.models import PortfolioState
from aitos.trading.lifecycle import TradeLifecycle


def make_portfolio(**overrides):
    defaults = dict(
        equity_usd=10_000.0, peak_equity_usd=10_000.0, volatility_percentile=30.0
    )
    defaults.update(overrides)
    return PortfolioState(**defaults)


def make_opportunity(**overrides):
    defaults = dict(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        stop_loss_price=98.0,
        take_profit_levels=[104.0],
        confidence=0.8,
        strategy_id="test-strategy",
        rationale="test",
    )
    defaults.update(overrides)
    return Opportunity(**defaults)


class FakeExchangeCapableExecutor(OrderExecutor):
    """Simulated executor that also supports exchange-side resting orders,
    tracking placed/cancelled orders and letting tests flip a resting
    order's status to FILLED to exercise reconciliation."""

    def __init__(self):
        self._order_counter = 0
        self.placed_orders = (
            {}
        )  # order_id -> dict(kind, symbol, side, quantity, price, status)
        self.cancelled_order_ids = []

    @property
    def supports_exchange_side_stops(self) -> bool:
        return True

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        self._order_counter += 1
        return OrderResult(
            order_id=f"entry-{self._order_counter}",
            symbol=request.symbol,
            side=request.side,
            filled_quantity=request.quantity,
            fill_price=request.reference_price,
        )

    async def place_stop_loss_order(
        self, symbol, side, quantity, stop_price
    ) -> OrderResult:
        return self._place_resting("sl", symbol, side, quantity, stop_price)

    async def place_take_profit_order(
        self, symbol, side, quantity, take_profit_price
    ) -> OrderResult:
        return self._place_resting("tp", symbol, side, quantity, take_profit_price)

    def _place_resting(self, kind, symbol, side, quantity, price) -> OrderResult:
        self._order_counter += 1
        order_id = f"{kind}-{self._order_counter}"
        self.placed_orders[order_id] = {
            "kind": kind,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "status": "NEW",
        }
        return OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=side,
            filled_quantity=0.0,
            fill_price=price,
            success=True,
        )

    async def cancel_resting_order(self, symbol: str, order_id: str) -> None:
        self.cancelled_order_ids.append(order_id)
        if order_id in self.placed_orders:
            self.placed_orders[order_id]["status"] = "CANCELED"

    async def get_resting_order_status(self, symbol: str, order_id: str):
        order = self.placed_orders.get(order_id)
        return order["status"] if order else None

    def mark_filled(self, order_id: str) -> None:
        self.placed_orders[order_id]["status"] = "FILLED"


@pytest.mark.asyncio
async def test_simulated_executor_does_not_support_exchange_side_stops():
    executor = SimulatedOrderExecutor()
    assert executor.supports_exchange_side_stops is False
    with pytest.raises(NotImplementedError):
        await executor.place_stop_loss_order("BTCUSDT", TradeSide.LONG, 1.0, 98.0)


@pytest.mark.asyncio
async def test_lifecycle_ignores_flag_when_executor_does_not_support_it(
    event_bus, risk_engine
):
    lifecycle = TradeLifecycle(
        event_bus=event_bus, risk_engine=risk_engine, use_exchange_side_stops=True
    )
    # SimulatedOrderExecutor doesn't support it, so the flag should be silently downgraded to False.
    assert lifecycle._use_exchange_side_stops is False


@pytest.mark.asyncio
async def test_opening_position_places_resting_sl_and_tp_orders(event_bus, risk_engine):
    executor = FakeExchangeCapableExecutor()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    await lifecycle.initialize({})

    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())

    assert trade.sl_order_id is not None
    assert len(trade.tp_order_ids) == 1
    assert executor.placed_orders[trade.sl_order_id]["kind"] == "sl"
    assert executor.placed_orders[trade.tp_order_ids[0]]["kind"] == "tp"


@pytest.mark.asyncio
async def test_multi_tp_places_split_quantity_resting_orders(event_bus, risk_engine):
    executor = FakeExchangeCapableExecutor()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    await lifecycle.initialize({})

    opportunity = make_opportunity(take_profit_levels=[102.0, 106.0])
    trade = await lifecycle.submit_opportunity(opportunity, make_portfolio())

    assert len(trade.tp_order_ids) == 2
    first_leg_qty = executor.placed_orders[trade.tp_order_ids[0]]["quantity"]
    second_leg_qty = executor.placed_orders[trade.tp_order_ids[1]]["quantity"]
    assert first_leg_qty == pytest.approx(trade.quantity * 0.5, rel=1e-6)
    assert second_leg_qty == pytest.approx(trade.quantity * 0.5, rel=1e-6)


@pytest.mark.asyncio
async def test_full_close_cancels_all_resting_orders(event_bus, risk_engine):
    executor = FakeExchangeCapableExecutor()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    await lifecycle.initialize({})

    trade = await lifecycle.submit_opportunity(
        make_opportunity(breakeven_at_r_multiple=None), make_portfolio()
    )
    sl_order_id = trade.sl_order_id
    tp_order_id = trade.tp_order_ids[0]

    await lifecycle.update_price(
        trade.trade_id, 105.0
    )  # hits the single TP, closes fully

    assert sl_order_id in executor.cancelled_order_ids
    assert tp_order_id in executor.cancelled_order_ids


@pytest.mark.asyncio
async def test_partial_tp_cancels_only_the_consumed_leg(event_bus, risk_engine):
    executor = FakeExchangeCapableExecutor()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    await lifecycle.initialize({})

    opportunity = make_opportunity(
        take_profit_levels=[102.0, 106.0], breakeven_at_r_multiple=None
    )
    trade = await lifecycle.submit_opportunity(opportunity, make_portfolio())
    first_tp_order_id, second_tp_order_id = trade.tp_order_ids

    await lifecycle.update_price(trade.trade_id, 102.5)  # hits first TP only

    assert first_tp_order_id in executor.cancelled_order_ids
    assert second_tp_order_id not in executor.cancelled_order_ids
    assert trade.tp_order_ids == [second_tp_order_id]


@pytest.mark.asyncio
async def test_breakeven_replaces_resting_stop_loss_order(event_bus, risk_engine):
    executor = FakeExchangeCapableExecutor()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    await lifecycle.initialize({})

    trade = await lifecycle.submit_opportunity(
        make_opportunity(breakeven_at_r_multiple=1.0), make_portfolio()
    )
    original_sl_order_id = trade.sl_order_id

    await lifecycle.update_price(
        trade.trade_id, 102.5
    )  # +1.25R, triggers breakeven, below TP

    assert original_sl_order_id in executor.cancelled_order_ids
    assert trade.sl_order_id != original_sl_order_id
    assert executor.placed_orders[trade.sl_order_id]["price"] == 100.0


@pytest.mark.asyncio
async def test_reconcile_trade_detects_exchange_side_sl_fill(event_bus, risk_engine):
    executor = FakeExchangeCapableExecutor()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    await lifecycle.initialize({})

    trade = await lifecycle.submit_opportunity(
        make_opportunity(breakeven_at_r_multiple=None), make_portfolio()
    )
    executor.mark_filled(trade.sl_order_id)

    reconciled = await lifecycle.reconcile_trade(trade.trade_id)

    assert reconciled.state == TradeLifecycleState.POSITION_CLOSED
    assert reconciled.exit_reason == "sl_triggered_exchange"


@pytest.mark.asyncio
async def test_reconcile_trade_detects_exchange_side_tp_fill(event_bus, risk_engine):
    executor = FakeExchangeCapableExecutor()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    await lifecycle.initialize({})

    trade = await lifecycle.submit_opportunity(
        make_opportunity(breakeven_at_r_multiple=None), make_portfolio()
    )
    tp_order_id = trade.tp_order_ids[0]
    executor.mark_filled(tp_order_id)

    reconciled = await lifecycle.reconcile_trade(trade.trade_id)

    assert reconciled.state == TradeLifecycleState.POSITION_CLOSED
    assert reconciled.exit_reason == "tp_triggered_exchange"
    assert reconciled.pnl > 0


@pytest.mark.asyncio
async def test_reconcile_trade_no_op_when_nothing_filled(event_bus, risk_engine):
    executor = FakeExchangeCapableExecutor()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    await lifecycle.initialize({})

    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    reconciled = await lifecycle.reconcile_trade(trade.trade_id)

    assert reconciled.state == TradeLifecycleState.POSITION_OPENED
    assert reconciled.trade_id in [t.trade_id for t in lifecycle.get_open_trades()]


@pytest.mark.asyncio
async def test_reconcile_trade_no_op_without_exchange_side_stops(
    event_bus, risk_engine
):
    lifecycle = TradeLifecycle(
        event_bus=event_bus, risk_engine=risk_engine
    )  # simulated, flag off
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())

    reconciled = await lifecycle.reconcile_trade(trade.trade_id)
    assert reconciled.state == TradeLifecycleState.POSITION_OPENED
