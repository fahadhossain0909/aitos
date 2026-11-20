from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aitos.execution.intent_store import ExecutionIntentStore
from aitos.execution.protection_reconciliation import recover_protection_handovers
from aitos.execution.order_executor import (
    OrderExecutor,
    OrderRequest,
    OrderResult,
    SimulatedOrderExecutor,
)
from aitos.intelligence.exit_intelligence.models import ExitAction
from aitos.models.trade import (
    Opportunity,
    Trade,
    TradeLifecycleState,
    TradeSide,
)
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
        self, symbol, side, quantity, stop_price, *, owner_trade_id=None
    ) -> OrderResult:
        return self._place_resting("sl", symbol, side, quantity, stop_price)

    async def place_take_profit_order(
        self, symbol, side, quantity, take_profit_price, *, owner_trade_id=None
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

    # Capital authorization must not pre-empt the lifecycle/exchange-stop
    # contract for a valid legacy opportunity with no capital metadata.
    assert trade.state == TradeLifecycleState.POSITION_OPENED
    assert trade.agent_consensus["capital_objective"]["eligible"] is True
    assert trade.sl_order_id is not None
    assert len(trade.tp_order_ids) == 1
    assert executor.placed_orders[trade.sl_order_id]["kind"] == "sl"
    assert executor.placed_orders[trade.tp_order_ids[0]]["kind"] == "tp"


@pytest.mark.asyncio
async def test_eie_uses_exchange_side_emergency_stop_not_soft_stop(
    event_bus, risk_engine
):
    from aitos.trading.position_manager import PositionManager

    executor = FakeExchangeCapableExecutor()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
        position_manager=PositionManager(),
    )
    await lifecycle.initialize({})

    trade = await lifecycle.submit_opportunity(
        make_opportunity(breakeven_at_r_multiple=None), make_portfolio()
    )
    placed_stop = executor.placed_orders[trade.sl_order_id]["price"]
    assert trade.sl_price == pytest.approx(98.0)
    assert trade.emergency_sl_price == pytest.approx(96.0)
    assert placed_stop == pytest.approx(trade.emergency_sl_price)
    # A resting TP must not bypass an EIE HOLD decision.
    assert trade.tp_order_ids == []


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


@pytest.mark.asyncio
async def test_cancel_failure_keeps_triggered_position_open_for_retry(
    event_bus, risk_engine
):
    class FailingCancelExecutor(FakeExchangeCapableExecutor):
        async def cancel_resting_order(self, symbol: str, order_id: str) -> None:
            raise RuntimeError("exchange cancel unavailable")

    executor = FailingCancelExecutor()
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

    with pytest.raises(RuntimeError, match="exchange cancel unavailable"):
        await lifecycle.update_price(trade.trade_id, 97.0)

    assert trade.trade_id in [t.trade_id for t in lifecycle.get_open_trades()]
    assert trade.state == TradeLifecycleState.POSITION_OPENED
    assert trade.trade_id not in [t.trade_id for t in lifecycle.get_closed_trades()]
    assert trade.sl_order_id is not None


@pytest.mark.asyncio
async def test_reconcile_filled_stop_does_not_cancel_already_filled_order(
    event_bus, risk_engine
):
    class RejectFilledCancelExecutor(FakeExchangeCapableExecutor):
        async def cancel_resting_order(self, symbol: str, order_id: str) -> None:
            if self.placed_orders.get(order_id, {}).get("status") == "FILLED":
                raise RuntimeError("cannot cancel filled order")
            await super().cancel_resting_order(symbol, order_id)

    executor = RejectFilledCancelExecutor()
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
    filled_sl = trade.sl_order_id
    executor.mark_filled(filled_sl)

    closed = await lifecycle.reconcile_trade(trade.trade_id)

    assert closed.state == TradeLifecycleState.POSITION_CLOSED
    assert closed.exit_reason == "sl_triggered_exchange"
    assert filled_sl not in executor.cancelled_order_ids


@pytest.mark.asyncio
async def test_reconcile_filled_tp_does_not_cancel_already_filled_order(
    event_bus, risk_engine
):
    class RejectFilledCancelExecutor(FakeExchangeCapableExecutor):
        async def cancel_resting_order(self, symbol: str, order_id: str) -> None:
            if self.placed_orders.get(order_id, {}).get("status") == "FILLED":
                raise RuntimeError("cannot cancel filled order")
            await super().cancel_resting_order(symbol, order_id)

    executor = RejectFilledCancelExecutor()
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
    filled_tp = trade.tp_order_ids[0]
    executor.mark_filled(filled_tp)

    closed = await lifecycle.reconcile_trade(trade.trade_id)

    assert closed.state == TradeLifecycleState.POSITION_CLOSED
    assert closed.exit_reason == "tp_triggered_exchange"
    assert filled_tp not in executor.cancelled_order_ids


@pytest.mark.asyncio
async def test_intelligent_dynamic_tp_extension_updates_local_checkpoint_only(
    event_bus, risk_engine
):
    extension = SimpleNamespace(
        approved=True,
        target_price=115.0,
        to_dict=lambda: {"approved": True, "target_price": 115.0},
    )
    decision = SimpleNamespace(
        action=ExitAction.HOLD,
        target_extension=extension,
        new_stop_price=None,
        to_dict=lambda: {"action": "HOLD", "target_extension": extension.to_dict()},
    )

    class FakePositionManager:
        def evaluate(self, **kwargs):
            return decision

    executor = FakeExchangeCapableExecutor()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
        position_manager=FakePositionManager(),
    )
    trade = Trade(
        trade_id="intelligent-tp-contract",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        quantity=1.0,
        leverage=1.0,
        position_size_usd=100.0,
        risk_amount_usd=2.0,
        strategy_id="test-strategy",
        agent_consensus={},
        explanation="EIE TP checkpoint contract",
        sl_price=98.0,
        tp_price=104.0,
        take_profit_levels=[104.0, 110.0],
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time="2026-11-05T00:00:00+00:00",
    )
    lifecycle._open_trades[trade.trade_id] = trade

    await lifecycle._place_exchange_side_stops(trade)
    assert [order["kind"] for order in executor.placed_orders.values()] == ["sl"]
    assert trade.tp_order_ids == []

    result = await lifecycle._apply_exit_intelligence(trade, 112.0)

    assert result is None
    assert trade.state == TradeLifecycleState.POSITION_OPENED
    assert trade.take_profit_levels == [104.0, 115.0]
    assert trade.tp_order_ids == []
    assert [order["kind"] for order in executor.placed_orders.values()] == ["sl"]


@pytest.mark.asyncio
async def test_intelligent_dynamic_tp_extension_veto_keeps_existing_target(
    event_bus, risk_engine
):
    extension = SimpleNamespace(
        approved=True,
        target_price=103.0,
        to_dict=lambda: {"approved": True, "target_price": 103.0},
    )
    decision = SimpleNamespace(
        action=ExitAction.HOLD,
        target_extension=extension,
        new_stop_price=None,
        to_dict=lambda: {"action": "HOLD", "target_extension": extension.to_dict()},
    )

    class FakePositionManager:
        def evaluate(self, **kwargs):
            return decision

    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=FakeExchangeCapableExecutor(),
        position_manager=FakePositionManager(),
    )
    trade = Trade(
        trade_id="intelligent-tp-veto",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        quantity=1.0,
        leverage=1.0,
        position_size_usd=100.0,
        risk_amount_usd=2.0,
        strategy_id="test-strategy",
        agent_consensus={},
        explanation="EIE TP extension veto",
        sl_price=98.0,
        tp_price=104.0,
        take_profit_levels=[104.0, 110.0],
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time="2026-11-05T00:00:00+00:00",
    )
    lifecycle._open_trades[trade.trade_id] = trade

    await lifecycle._apply_exit_intelligence(trade, 112.0)

    assert trade.take_profit_levels == [104.0, 110.0]
    assert trade.state == TradeLifecycleState.POSITION_OPENED

class LiveTakeProfitHandoverExecutor(FakeExchangeCapableExecutor):
    execution_scope = "account"
    hedge_mode = False

    def __init__(self, journal):
        super().__init__()
        self._journal = journal
        self.statuses = {"algo:123": "NEW"}
        self.cancel_calls = []

    @property
    def requires_exchange_execution(self):
        return True

    @property
    def intent_store(self):
        return self._journal

    async def place_take_profit_order(
        self,
        symbol,
        side,
        quantity,
        take_profit_price,
        *,
        owner_trade_id=None,
        client_order_id=None,
    ):
        request = OrderRequest(
            symbol=symbol,
            side=side,
            quantity=quantity,
            reference_price=take_profit_price,
            order_type="TAKE_PROFIT_MARKET",
            client_order_id=client_order_id,
            reduce_only=True,
            owner_trade_id=owner_trade_id,
        )
        self._journal.begin(request)
        result = OrderResult(
            "algo:456",
            symbol,
            side,
            0.0,
            take_profit_price,
            client_order_id=client_order_id,
        )
        self._journal.record_protection(client_order_id, result)
        self.statuses[result.order_id] = "NEW"
        return result

    async def get_resting_order_status(self, symbol, order_id):
        return self.statuses.get(order_id)

    async def get_resting_order_status(self, symbol, order_id):
        return self.statuses.get(order_id)

    async def cancel_resting_order(self, symbol, order_id):
        self.cancel_calls.append(order_id)
        self.statuses[order_id] = "CANCELED"

    async def get_algo_order_by_client_id(self, symbol, client_order_id):
        return {
            "algoId": 456,
            "clientAlgoId": client_order_id,
            "symbol": symbol,
            "side": "SELL",
            "orderType": "TAKE_PROFIT_MARKET",
            "algoStatus": "NEW",
            "quantity": "0.5",
            "triggerPrice": "110.0",
            "positionSide": "BOTH",
            "reduceOnly": True,
        }

    def acknowledge_protection(self, result, trade_id):
        self._journal.mark_applied(result.client_order_id, trade_id)


def seed_owned_take_profit(journal, trade_id="trade-tp"):
    request = OrderRequest(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=0.5,
        reference_price=104.0,
        order_type="TAKE_PROFIT_MARKET",
        client_order_id="old-tp-client",
        reduce_only=True,
        owner_trade_id=trade_id,
    )
    journal.begin(request)
    journal.record_protection(
        "old-tp-client",
        OrderResult(
            "algo:123",
            "BTCUSDT",
            TradeSide.LONG,
            0.0,
            104.0,
            client_order_id="old-tp-client",
        ),
    )
    journal.mark_applied("old-tp-client", trade_id)


@pytest.mark.asyncio
async def test_live_take_profit_replacement_is_make_before_break(
    event_bus, risk_engine, tmp_path
):
    journal = ExecutionIntentStore(tmp_path / "tp-handover.sqlite", scope="account")
    seed_owned_take_profit(journal)
    executor = LiveTakeProfitHandoverExecutor(journal)
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    trade = SimpleNamespace(
        trade_id="trade-tp",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        tp_order_ids=["algo:123"],
        take_profit_levels=[104.0],
        sl_order_id="algo:999",
        agent_consensus={},
        updated_at="",
    )
    lifecycle._open_trades[trade.trade_id] = trade

    async def persist(_trade):
        return None

    lifecycle._execution_state_writer = persist
    lifecycle._execution_position_verifier = AsyncMock()
    await lifecycle._replace_exchange_side_take_profit(trade, 0, 110.0)

    assert trade.tp_order_ids == ["algo:456"]
    assert trade.take_profit_levels == [110.0]
    assert executor.cancel_calls == ["algo:123"]
    assert journal.protection_handovers()[0]["state"] == "APPLIED"
    assert journal.protection_handovers()[0]["order_type"] == "TAKE_PROFIT_MARKET"
    assert journal.protection_intents()[0]["state"] == "APPLIED"


@pytest.mark.asyncio
async def test_live_take_profit_replacement_keeps_old_order_if_snapshot_write_fails(
    event_bus, risk_engine, tmp_path
):
    journal = ExecutionIntentStore(tmp_path / "tp-handover-fault.sqlite", scope="account")
    seed_owned_take_profit(journal)
    executor = LiveTakeProfitHandoverExecutor(journal)
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    trade = SimpleNamespace(
        trade_id="trade-tp",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        tp_order_ids=["algo:123"],
        take_profit_levels=[104.0],
        sl_order_id="algo:999",
        agent_consensus={},
        updated_at="",
    )
    lifecycle._open_trades[trade.trade_id] = trade

    async def fail_persist(_trade):
        raise OSError("simulated durable snapshot failure")

    lifecycle._execution_state_writer = fail_persist
    lifecycle._execution_position_verifier = AsyncMock()
    with pytest.raises(OSError, match="snapshot failure"):
        await lifecycle._replace_exchange_side_take_profit(trade, 0, 110.0)

    assert trade.tp_order_ids == ["algo:456"]
    assert executor.cancel_calls == []
    assert journal.protection_handovers()[0]["state"] == "NEW_ACCEPTED"

@pytest.mark.asyncio
async def test_tp_handover_restart_after_cancel_ack_does_not_cancel_twice(
    event_bus, risk_engine, tmp_path
):
    journal = ExecutionIntentStore(tmp_path / "tp-handover-after-cancel.sqlite", scope="account")
    seed_owned_take_profit(journal)
    executor = LiveTakeProfitHandoverExecutor(journal)
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
        use_exchange_side_stops=True,
    )
    trade = SimpleNamespace(
        trade_id="trade-tp",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        tp_order_ids=["algo:123"],
        take_profit_levels=[104.0],
        sl_order_id="algo:999",
        agent_consensus={},
        updated_at="",
    )
    lifecycle._open_trades[trade.trade_id] = trade
    writes = 0
    durable_snapshots = []

    async def fail_after_cancel(_trade):
        nonlocal writes
        writes += 1
        durable_snapshots.append(
            {
                "tp_order_ids": list(_trade.tp_order_ids),
                "take_profit_levels": list(_trade.take_profit_levels),
                "agent_consensus": dict(_trade.agent_consensus),
            }
        )
        if writes == 2:
            raise OSError("simulated post-cancel snapshot failure")

    lifecycle._execution_state_writer = fail_after_cancel
    lifecycle._execution_position_verifier = AsyncMock()
    with pytest.raises(OSError, match="post-cancel"):
        await lifecycle._replace_exchange_side_take_profit(trade, 0, 110.0)

    assert executor.cancel_calls == ["algo:123"]
    assert journal.protection_handovers()[0]["state"] == "OLD_CANCELLED"
    assert durable_snapshots[0]["agent_consensus"]["_pending_stop_cancellations"] == [
        "algo:123"
    ]

    restored = SimpleNamespace(
        trade_id="trade-tp",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=1.0,
        tp_order_ids=["algo:456"],
        take_profit_levels=[110.0],
        sl_order_id="algo:999",
        agent_consensus=durable_snapshots[0]["agent_consensus"],
        updated_at="",
    )

    async def persist(_trade):
        return None

    report = await recover_protection_handovers(
        executor, journal, [restored], persist
    )
    assert report["recovered"] == 1
    assert report["blocked"] == []
    assert restored.tp_order_ids == ["algo:456"]
    assert restored.agent_consensus["_pending_stop_cancellations"] == []
    assert executor.cancel_calls == ["algo:123"]
    assert journal.protection_handovers()[0]["state"] == "APPLIED"
