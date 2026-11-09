import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from aitos.core.contracts import Event
from aitos.core.exceptions import TradeNotFoundError
from aitos.execution.order_executor import OrderExecutor, OrderResult
from aitos.models.trade import Opportunity, TradeLifecycleState, TradeSide
from aitos.risk.models import PortfolioState, PositionExposure
from aitos.statistics.shadow import StatisticalShadowLedger
from aitos.trading.lifecycle import TradeLifecycle


def make_portfolio(**overrides) -> PortfolioState:
    defaults = dict(
        equity_usd=10_000.0,
        peak_equity_usd=10_000.0,
        volatility_percentile=30.0,
        max_pairwise_correlation=0.1,
    )
    defaults.update(overrides)
    return PortfolioState(**defaults)


def make_opportunity(**overrides) -> Opportunity:
    defaults = dict(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        stop_loss_price=98.0,
        take_profit_levels=[104.0],
        confidence=0.8,
        strategy_id="test-strategy",
        rationale="test rationale",
    )
    defaults.update(overrides)
    return Opportunity(**defaults)


@pytest.mark.asyncio
async def test_healthy_opportunity_opens_a_position(event_bus, risk_engine):
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    assert trade.state == TradeLifecycleState.POSITION_OPENED
    assert trade.entry_price == 100.0
    assert trade.sl_price == 98.0
    assert trade.quantity > 0
    assert trade.leverage >= 1.0
    assert trade.trade_id in [t.trade_id for t in lifecycle.get_open_trades()]


@pytest.mark.asyncio
async def test_full_lifecycle_publishes_expected_event_sequence(event_bus, risk_engine):
    received_topics = []

    async def handler(event: Event):
        received_topics.append(event.topic)

    for topic in [
        "decision.opportunity",
        "decision.entry",
        "trade.order_submitted",
        "trade.order_filled",
        "trade.position_opened",
    ]:
        await event_bus.subscribe(topic, handler, group="test")
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    for _ in range(30):
        if len(received_topics) >= 5:
            break
        await asyncio.sleep(0.1)
    assert set(received_topics) == {
        "decision.opportunity",
        "decision.entry",
        "trade.order_submitted",
        "trade.order_filled",
        "trade.position_opened",
    }


@pytest.mark.asyncio
async def test_risk_veto_rejects_opportunity(event_bus, risk_engine):
    await risk_engine.trigger_emergency_stop("manual test trip")
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    assert trade.state == TradeLifecycleState.REJECTED
    assert "risk veto" in trade.rejection_reason
    assert trade.trade_id not in [t.trade_id for t in lifecycle.get_open_trades()]


@pytest.mark.asyncio
async def test_hard_limit_breach_rejects_opportunity(event_bus, risk_engine):
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    breached_portfolio = make_portfolio(equity_usd=7000.0, peak_equity_usd=10_000.0)
    trade = await lifecycle.submit_opportunity(make_opportunity(), breached_portfolio)
    assert trade.state == TradeLifecycleState.REJECTED
    assert "hard limit breach" in trade.rejection_reason


@pytest.mark.asyncio
async def test_production_opportunity_without_approval_is_rejected(
    event_bus, risk_engine, kernel
):
    lifecycle = TradeLifecycle(
        event_bus=event_bus, risk_engine=risk_engine, kernel=kernel
    )
    await lifecycle.initialize({})
    opportunity = make_opportunity(is_production=True, approved_by=None)
    trade = await lifecycle.submit_opportunity(opportunity, make_portfolio())
    assert trade.state == TradeLifecycleState.REJECTED
    assert "governance denied" in trade.rejection_reason


@pytest.mark.asyncio
async def test_production_opportunity_with_approval_opens(
    event_bus, risk_engine, kernel
):
    lifecycle = TradeLifecycle(
        event_bus=event_bus, risk_engine=risk_engine, kernel=kernel
    )
    await lifecycle.initialize({})
    opportunity = make_opportunity(is_production=True, approved_by="fahad")
    trade = await lifecycle.submit_opportunity(opportunity, make_portfolio())
    assert trade.state == TradeLifecycleState.POSITION_OPENED


@pytest.mark.asyncio
async def test_stop_loss_hit_closes_trade_at_loss(event_bus, risk_engine):
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(
        make_opportunity(breakeven_at_r_multiple=None), make_portfolio()
    )
    closed = await lifecycle.update_price(trade.trade_id, 97.5)
    assert closed.state == TradeLifecycleState.POSITION_CLOSED
    assert closed.exit_reason == "sl_triggered"
    assert closed.pnl < 0
    assert closed.trade_id not in [t.trade_id for t in lifecycle.get_open_trades()]
    assert closed.trade_id in [t.trade_id for t in lifecycle.get_closed_trades()]


@pytest.mark.asyncio
async def test_exit_intelligence_can_hold_through_soft_stop_but_not_emergency_limit(
    event_bus, risk_engine
):
    from aitos.intelligence.exit_intelligence import ExitAction
    from aitos.trading.position_manager import PositionAction

    class HoldingManager:
        calls = 0

        def evaluate(self, **kwargs):
            self.calls += 1
            return PositionAction(action=ExitAction.HOLD, reason="entry thesis intact")

        def clear_trade(self, trade_id, symbol=None):
            return None

    manager = HoldingManager()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        position_manager=manager,
    )
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(
        make_opportunity(breakeven_at_r_multiple=None), make_portfolio()
    )

    held = await lifecycle.update_price(trade.trade_id, 97.5)
    assert held.state == TradeLifecycleState.POSITION_OPENED
    assert held.exit_reason is None
    assert manager.calls == 1

    # Remaining below the soft stop does not force deep analysis on every tick.
    still_held = await lifecycle.update_price(trade.trade_id, 97.4)
    assert still_held.state == TradeLifecycleState.POSITION_OPENED
    assert manager.calls == 1

    emergency = await lifecycle.update_price(trade.trade_id, 96.0)
    assert emergency.state == TradeLifecycleState.POSITION_CLOSED
    assert emergency.exit_reason == "emergency_risk_limit"
    # Emergency risk protection is independent of the HOLD decision.
    assert manager.calls == 1


@pytest.mark.asyncio
async def test_static_final_tp_does_not_override_eie_manage(event_bus, risk_engine):
    from aitos.intelligence.exit_intelligence import ExitAction
    from aitos.trading.position_manager import PositionAction

    class ManagingManager:
        def evaluate(self, **kwargs):
            return PositionAction(
                action=ExitAction.MANAGE,
                reason="thesis remains valid; reduce exposure",
                reduce_fraction=0.25,
            )

        def clear_trade(self, trade_id, symbol=None):
            return None

    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        position_manager=ManagingManager(),
    )
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(
        make_opportunity(breakeven_at_r_multiple=None), make_portfolio()
    )

    managed = await lifecycle.update_price(trade.trade_id, 105.0)
    assert managed.state == TradeLifecycleState.POSITION_OPENED
    assert len(managed.partial_exits) == 1
    assert managed.exit_reason is None
    assert managed._last_eie_action == "MANAGE"


@pytest.mark.asyncio
async def test_eie_tp_checkpoints_never_auto_exit_or_scale_out(event_bus, risk_engine):
    from aitos.intelligence.exit_intelligence import ExitAction
    from aitos.trading.position_manager import PositionAction

    class HoldingManager:
        calls = 0

        def evaluate(self, **kwargs):
            self.calls += 1
            return PositionAction(
                action=ExitAction.HOLD, reason="thesis still extending"
            )

        def clear_trade(self, trade_id, symbol=None):
            return None

    manager = HoldingManager()
    lifecycle = TradeLifecycle(
        event_bus=event_bus, risk_engine=risk_engine, position_manager=manager
    )
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(
        make_opportunity(
            take_profit_levels=[102.0, 106.0, 112.0],
            breakeven_at_r_multiple=None,
        ),
        make_portfolio(),
    )
    original_quantity = trade.quantity
    original_size = trade.position_size_usd

    first = await lifecycle.update_price(trade.trade_id, 103.0)
    assert first.state == TradeLifecycleState.POSITION_OPENED
    assert first.take_profit_levels == [106.0, 112.0]
    assert first.quantity == pytest.approx(original_quantity)
    assert first.position_size_usd == pytest.approx(original_size)
    assert first.partial_exits == []
    assert manager.calls == 1

    second = await lifecycle.update_price(trade.trade_id, 107.0)
    assert second.state == TradeLifecycleState.POSITION_OPENED
    assert second.take_profit_levels == [112.0]
    assert second.quantity == pytest.approx(original_quantity)
    assert second.partial_exits == []
    assert manager.calls == 2


@pytest.mark.asyncio
async def test_eie_can_exit_at_tp_checkpoint_only_when_it_decides_to(
    event_bus, risk_engine
):
    from aitos.intelligence.exit_intelligence import ExitAction
    from aitos.trading.position_manager import PositionAction

    class ExitingManager:
        def evaluate(self, **kwargs):
            return PositionAction(
                action=ExitAction.EXIT, reason="momentum and thesis invalidated"
            )

        def clear_trade(self, trade_id, symbol=None):
            return None

    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        position_manager=ExitingManager(),
    )
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(
        make_opportunity(breakeven_at_r_multiple=None), make_portfolio()
    )

    closed = await lifecycle.update_price(trade.trade_id, 105.0)
    assert closed.state == TradeLifecycleState.POSITION_CLOSED
    assert closed.exit_reason == "momentum and thesis invalidated"
    assert closed.pnl > 0


@pytest.mark.asyncio
async def test_short_position_can_hold_through_soft_stop_until_emergency_limit(
    event_bus, risk_engine
):
    from aitos.intelligence.exit_intelligence import ExitAction
    from aitos.trading.position_manager import PositionAction

    class HoldingManager:
        def evaluate(self, **kwargs):
            return PositionAction(action=ExitAction.HOLD, reason="short thesis intact")

        def clear_trade(self, trade_id, symbol=None):
            return None

    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        position_manager=HoldingManager(),
    )
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(
        make_opportunity(
            side=TradeSide.SHORT,
            entry_price=100.0,
            stop_loss_price=102.0,
            take_profit_levels=[96.0],
            breakeven_at_r_multiple=None,
        ),
        make_portfolio(),
    )

    held = await lifecycle.update_price(trade.trade_id, 102.5)
    assert held.state == TradeLifecycleState.POSITION_OPENED
    assert held.emergency_sl_price == pytest.approx(104.0)

    closed = await lifecycle.update_price(trade.trade_id, 104.0)
    assert closed.state == TradeLifecycleState.POSITION_CLOSED
    assert closed.exit_reason == "emergency_risk_limit"


@pytest.mark.asyncio
async def test_soft_stop_fails_closed_when_exit_intelligence_is_unavailable(
    event_bus, risk_engine
):
    class FailingManager:
        def evaluate(self, **kwargs):
            raise RuntimeError("EIE unavailable")

        def clear_trade(self, trade_id, symbol=None):
            return None

    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        position_manager=FailingManager(),
    )
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(
        make_opportunity(breakeven_at_r_multiple=None), make_portfolio()
    )
    closed = await lifecycle.update_price(trade.trade_id, 97.5)
    assert closed.state == TradeLifecycleState.POSITION_CLOSED
    assert closed.exit_reason == "sl_triggered_eie_unavailable"


@pytest.mark.asyncio
async def test_exit_intelligence_exit_closes_at_soft_stop_boundary(
    event_bus, risk_engine
):
    from aitos.intelligence.exit_intelligence import ExitAction
    from aitos.trading.position_manager import PositionAction

    class ExitingManager:
        def evaluate(self, **kwargs):
            return PositionAction(
                action=ExitAction.EXIT, reason="entry thesis invalidated"
            )

        def clear_trade(self, trade_id, symbol=None):
            return None

    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        position_manager=ExitingManager(),
    )
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(
        make_opportunity(breakeven_at_r_multiple=None), make_portfolio()
    )
    closed = await lifecycle.update_price(trade.trade_id, 97.5)
    assert closed.state == TradeLifecycleState.POSITION_CLOSED
    assert closed.exit_reason == "entry thesis invalidated"


@pytest.mark.asyncio
async def test_intensive_exit_analysis_cadence_is_per_position(event_bus, risk_engine):
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    assert lifecycle._allow_intensive_analysis("trade-a") is True
    assert lifecycle._allow_intensive_analysis("trade-a") is False
    # A busy position cannot consume another position's evaluation slot.
    assert lifecycle._allow_intensive_analysis("trade-b") is True


@pytest.mark.asyncio
async def test_entry_thesis_is_captured_at_position_open(event_bus, risk_engine):
    from aitos.trading.position_manager import PositionManager

    manager = PositionManager()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        position_manager=manager,
    )
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    assert trade.entry_thesis is not None
    assert trade.entry_thesis["trade_id"] == trade.trade_id
    assert trade.entry_thesis["entry_price"] == trade.entry_price
    assert trade.entry_thesis["created_at"] is not None
    assert manager._theses[trade.trade_id].entry_price == trade.entry_price


@pytest.mark.asyncio
async def test_take_profit_hit_closes_trade_at_profit(event_bus, risk_engine):
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(
        make_opportunity(breakeven_at_r_multiple=None), make_portfolio()
    )
    closed = await lifecycle.update_price(trade.trade_id, 105.0)
    assert closed.state == TradeLifecycleState.POSITION_CLOSED
    assert closed.exit_reason == "tp_triggered"
    assert closed.pnl > 0


@pytest.mark.asyncio
async def test_multi_tp_partial_close_then_final_close(event_bus, risk_engine):
    opportunity = make_opportunity(
        take_profit_levels=[102.0, 106.0], breakeven_at_r_multiple=None
    )
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(opportunity, make_portfolio())
    original_size = trade.position_size_usd
    still_open = await lifecycle.update_price(trade.trade_id, 102.5)
    assert still_open.state == TradeLifecycleState.POSITION_OPENED
    assert len(still_open.partial_exits) == 1
    assert still_open.position_size_usd < original_size
    assert still_open.take_profit_levels == [106.0]
    closed = await lifecycle.update_price(trade.trade_id, 107.0)
    assert closed.state == TradeLifecycleState.POSITION_CLOSED
    assert closed.exit_reason == "tp_triggered"
    assert closed.pnl > 0


@pytest.mark.asyncio
async def test_breakeven_trigger_moves_stop_to_entry(event_bus, risk_engine):
    opportunity = make_opportunity(breakeven_at_r_multiple=1.0)
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(opportunity, make_portfolio())
    updated = await lifecycle.update_price(trade.trade_id, 102.5)
    assert updated.state == TradeLifecycleState.POSITION_OPENED
    assert updated.sl_price == 100.0
    assert updated.breakeven_triggered is True


@pytest.mark.asyncio
async def test_trailing_sl_tightens_as_price_moves_favorably(event_bus, risk_engine):
    opportunity = make_opportunity(
        trailing_sl_enabled=True,
        breakeven_at_r_multiple=None,
        take_profit_levels=[150.0],
    )
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(opportunity, make_portfolio())
    initial_sl = trade.sl_price
    updated = await lifecycle.update_price(trade.trade_id, 110.0)
    assert updated.sl_price > initial_sl
    assert updated.state == TradeLifecycleState.POSITION_OPENED


@pytest.mark.asyncio
async def test_failed_order_submission_rejects_trade_instead_of_opening_phantom_position(
    event_bus, risk_engine
):
    from aitos.execution.order_executor import OrderExecutor, OrderResult

    class FailingExecutor(OrderExecutor):
        async def submit_order(self, request):
            return OrderResult(
                order_id="failed-1",
                symbol=request.symbol,
                side=request.side,
                filled_quantity=0.0,
                fill_price=0.0,
                success=False,
                error="insufficient margin",
            )

    lifecycle = TradeLifecycle(
        event_bus=event_bus, risk_engine=risk_engine, order_executor=FailingExecutor()
    )
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    assert trade.state == TradeLifecycleState.REJECTED
    assert "order submission failed" in trade.rejection_reason
    assert "insufficient margin" in trade.rejection_reason
    assert lifecycle.get_open_trades() == []


@pytest.mark.asyncio
async def test_update_price_on_unknown_trade_raises(event_bus, risk_engine):
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    with pytest.raises(TradeNotFoundError):
        await lifecycle.update_price("nonexistent", 100.0)


@pytest.mark.asyncio
async def test_close_trade_twice_raises_not_found(event_bus, risk_engine):
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    await lifecycle.close_trade(trade.trade_id, 101.0, "manual")
    with pytest.raises(TradeNotFoundError):
        await lifecycle.close_trade(trade.trade_id, 101.0, "manual")


@pytest.mark.asyncio
async def test_handle_event_auto_updates_matching_open_trade(event_bus, risk_engine):
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(
        make_opportunity(breakeven_at_r_multiple=None), make_portfolio()
    )
    kline_event = Event(
        topic="market.kline.BTCUSDT.1m",
        payload={"symbol": "BTCUSDT", "close": 97.0},
        source_module="test",
    )
    await lifecycle.handle_event(kline_event)
    assert trade.trade_id not in [t.trade_id for t in lifecycle.get_open_trades()]
    assert trade.trade_id in [t.trade_id for t in lifecycle.get_closed_trades()]


@pytest.mark.asyncio
async def test_existing_sector_hard_cap_rejects_candidate_before_order(
    event_bus, risk_engine
):
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    opportunity = make_opportunity(
        symbol="BNBUSDT", entry_price=100.0, stop_loss_price=99.0
    )
    portfolio = make_portfolio(
        # 41% existing exchange-token notional is above the configured 40% absolute hard cap.
        positions=(
            PositionExposure(symbol="BNBUSDT", notional_usd=4100.0, leverage=1.0),
        ),
    )
    trade = await lifecycle.submit_opportunity(opportunity, portfolio)
    assert trade.state == TradeLifecycleState.REJECTED
    assert "hard limit breach" in trade.rejection_reason
    assert lifecycle.get_open_trades() == []


@pytest.mark.asyncio
async def test_exit_event_publish_failure_does_not_stick_trade_in_exit_triggered(
    event_bus, risk_engine, monkeypatch
):
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(
        make_opportunity(breakeven_at_r_multiple=None), make_portfolio()
    )
    original_publish = event_bus.publish

    async def fail_stop_event(event):
        if event.topic == "trade.sl_triggered":
            raise RuntimeError("event bus unavailable")
        return await original_publish(event)

    monkeypatch.setattr(event_bus, "publish", fail_stop_event)

    with pytest.raises(RuntimeError, match="event bus unavailable"):
        await lifecycle.update_price(trade.trade_id, 97.0)

    assert trade.trade_id in [t.trade_id for t in lifecycle.get_open_trades()]
    assert trade.state == TradeLifecycleState.POSITION_OPENED


@pytest.mark.asyncio
async def test_partial_close_preserves_full_precision_notional_and_quantity(
    event_bus, risk_engine
):
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(
        make_opportunity(breakeven_at_r_multiple=None), make_portfolio()
    )
    initial_notional = trade.position_size_usd
    initial_quantity = trade.quantity

    await lifecycle._partial_close(trade, 101.25, 0.333333)

    closed_notional = trade.partial_exits[-1].size_usd
    closed_quantity = initial_quantity * 0.333333
    assert trade.position_size_usd + closed_notional == pytest.approx(
        initial_notional, rel=1e-12
    )
    assert trade.quantity + closed_quantity == pytest.approx(
        initial_quantity, rel=1e-12
    )
    assert trade.position_size_usd > 0
    assert trade.quantity > 0


@pytest.mark.asyncio
async def test_partial_exit_guard_enforces_count_cooldown_and_dust(
    event_bus, risk_engine
):
    import time

    from aitos.models.trade import PartialExit

    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(
        make_opportunity(breakeven_at_r_multiple=None), make_portfolio()
    )

    allowed, reason = lifecycle._partial_exit_allowed(trade, 0.25)
    assert allowed is True
    assert reason == "confirmed_partial_exit"

    lifecycle._last_partial_exit_at[trade.trade_id] = time.monotonic()
    allowed, reason = lifecycle._partial_exit_allowed(trade, 0.25)
    assert allowed is False
    assert reason == "partial_exit_cooldown"

    lifecycle._last_partial_exit_at.pop(trade.trade_id)
    trade.partial_exits.extend(
        PartialExit(price=101.0, size_usd=1.0, r_multiple=0.5) for _ in range(3)
    )
    allowed, reason = lifecycle._partial_exit_allowed(trade, 0.25)
    assert allowed is False
    assert reason == "partial_exit_count_limit"

    trade.partial_exits.clear()
    trade.position_size_usd = 1.1
    allowed, reason = lifecycle._partial_exit_allowed(trade, 0.5)
    assert allowed is False
    assert reason == "partial_exit_would_create_dust"


@pytest.mark.asyncio
async def test_fee_and_funding_adjustments_are_idempotently_included_in_net_pnl(
    event_bus, risk_engine
):
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(
        make_opportunity(breakeven_at_r_multiple=None), make_portfolio()
    )

    await lifecycle.apply_cost_adjustment(
        trade.trade_id,
        adjustment_id="entry-fee-fill-1",
        kind="fee",
        amount_usd=0.12,
    )
    await lifecycle.apply_cost_adjustment(
        trade.trade_id,
        adjustment_id="funding-payment-1",
        kind="funding",
        amount_usd=-0.03,
    )
    # Duplicate delivery must not double count either cost.
    await lifecycle.apply_cost_adjustment(
        trade.trade_id,
        adjustment_id="entry-fee-fill-1",
        kind="fee",
        amount_usd=0.12,
    )

    closed = await lifecycle.close_trade(trade.trade_id, 102.0, "test_close")
    expected_net = closed.gross_realized_pnl_usd - 0.12 - 0.03
    assert closed.fees_paid_usd == pytest.approx(0.12)
    assert closed.funding_pnl_usd == pytest.approx(-0.03)
    assert closed.pnl == pytest.approx(expected_net)
    assert len(closed.cost_adjustments) == 2


@pytest.mark.asyncio
async def test_lifecycle_routes_external_funding_adjustment_events(
    event_bus, risk_engine
):
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())

    await lifecycle.handle_event(
        Event(
            topic="trade.cost_adjustment",
            payload={
                "trade_id": trade.trade_id,
                "adjustment_id": "funding-event-42",
                "kind": "funding",
                "amount_usd": 0.08,
            },
            source_module="exchange-reconciliation",
        )
    )

    assert trade.funding_pnl_usd == pytest.approx(0.08)
    assert "funding-event-42" in trade.cost_adjustments


@pytest.mark.asyncio
async def test_statistical_forecast_reduces_size_and_is_persisted(
    event_bus, risk_engine
):
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    baseline = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    assert baseline.state == TradeLifecycleState.POSITION_OPENED
    await lifecycle.close_trade(
        baseline.trade_id, baseline.entry_price, "statistical_overlay_baseline"
    )
    forecast = {
        "symbol": "BTCUSDT",
        "horizon": "15m",
        "side": "LONG",
        "model_id": "aitos-contract-statistical-stack",
        "model_version": "1",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "window_start": "2026-11-07T04:00:00+00:00",
        "feature_snapshot_id": "snapshot-risk-overlay-test",
        "sample_size": 256,
        "predicted_up": 0.8,
        "downside_probability": 0.2,
        "forecast_volatility": 0.04,
        "reference_volatility": 0.02,
        "tail_probability": 0.20,
        "expected_shortfall": 0.05,
        "expected_return": 0.01,
        "probability_confidence": 0.8,
    }
    reduced = await lifecycle.submit_opportunity(
        make_opportunity(
            timeframe="15m",
            agent_consensus={"statistical_forecast": forecast},
        ),
        make_portfolio(),
    )
    assessment = reduced.agent_consensus["statistical_risk_assessment"]
    assert reduced.state == TradeLifecycleState.POSITION_OPENED
    assert 0.0 < assessment["size_multiplier"] < 1.0
    assert reduced.position_size_usd < baseline.position_size_usd
    assert reduced.risk_amount_usd < baseline.risk_amount_usd
    assert reduced.agent_consensus["statistical_forecast"] == forecast
    assert "forecast_volatility_above_reference" in assessment["reasons"]


@pytest.mark.asyncio
async def test_statistical_forecast_mismatch_or_staleness_fails_closed(
    event_bus, risk_engine
):
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    stale = {
        "symbol": "BTCUSDT",
        "horizon": "15m",
        "side": "LONG",
        "model_id": "aitos-contract-statistical-stack",
        "model_version": "1",
        "as_of": (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(),
        "sample_size": 256,
        "predicted_up": 0.8,
        "downside_probability": 0.2,
        "expected_return": 0.01,
        "forecast_volatility": 0.04,
        "reference_volatility": 0.02,
        "tail_probability": 0.20,
        "expected_shortfall": 0.05,
    }
    stale_trade = await lifecycle.submit_opportunity(
        make_opportunity(
            timeframe="15m",
            agent_consensus={"statistical_forecast": stale},
        ),
        make_portfolio(),
    )
    assert stale_trade.state == TradeLifecycleState.REJECTED
    assert "statistical forecast is stale" in stale_trade.rejection_reason

    fresh = dict(stale, as_of=datetime.now(timezone.utc).isoformat())
    mismatch_trade = await lifecycle.submit_opportunity(
        make_opportunity(
            timeframe="1h",
            agent_consensus={"statistical_forecast": fresh},
        ),
        make_portfolio(),
    )
    assert mismatch_trade.state == TradeLifecycleState.REJECTED
    assert "symbol, side or horizon mismatch" in mismatch_trade.rejection_reason


@pytest.mark.asyncio
async def test_statistical_shadow_ledger_resolves_trade_outcome(
    event_bus, risk_engine, tmp_path
):
    ledger = StatisticalShadowLedger(tmp_path / "statistical-shadow.sqlite3")
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        statistical_shadow_ledger=ledger,
    )
    await lifecycle.initialize({})
    now = datetime.now(timezone.utc)
    forecast = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "horizon": "15m",
        "model_id": "aitos-contract-statistical-stack",
        "model_version": "1",
        "as_of": now.isoformat(),
        "window_start": (now - timedelta(hours=1)).isoformat(),
        "feature_snapshot_id": "snapshot-lifecycle-1",
        "sample_size": 256,
        "predicted_up": 0.8,
        "downside_probability": 0.2,
        "forecast_volatility": 0.02,
        "reference_volatility": 0.01,
        "tail_probability": 0.1,
        "expected_shortfall": 0.03,
        "expected_return": 0.01,
        "probability_confidence": 0.8,
    }
    trade = await lifecycle.submit_opportunity(
        make_opportunity(
            timeframe="15m",
            agent_consensus={"statistical_forecast": forecast, "a_stat": 8.0},
        ),
        make_portfolio(),
    )
    assert trade.state == TradeLifecycleState.POSITION_OPENED
    pending = ledger.metrics(
        symbol="BTCUSDT",
        horizon="15m",
        model_id="aitos-contract-statistical-stack",
        model_version="1",
        side="LONG",
    )
    assert pending.observations == 0

    closed = await lifecycle.close_trade(
        trade.trade_id, trade.entry_price * 1.01, "shadow_outcome_test"
    )
    assert closed.state == TradeLifecycleState.POSITION_CLOSED
    resolved = ledger.metrics(
        symbol="BTCUSDT",
        horizon="15m",
        model_id="aitos-contract-statistical-stack",
        model_version="1",
        side="LONG",
    )
    assert resolved.observations == 1
    assert resolved.directional_hit_rate == 1.0
    assert resolved.brier == pytest.approx(0.04)


class _PartialFillExecutor(OrderExecutor):
    def __init__(
        self, fraction=0.4, fill_price_multiplier=1.001, success=True, error=None
    ):
        self.fraction = fraction
        self.fill_price_multiplier = fill_price_multiplier
        self.success = success
        self.error = error
        self.request = None

    async def submit_order(self, request):
        self.request = request
        return OrderResult(
            order_id="partial-fill-1",
            symbol=request.symbol,
            side=request.side,
            filled_quantity=request.quantity * self.fraction,
            fill_price=request.reference_price * self.fill_price_multiplier,
            success=self.success,
            error=self.error,
        )


@pytest.mark.asyncio
async def test_lifecycle_scales_position_and_risk_to_actual_partial_fill(
    event_bus, risk_engine
):
    executor = _PartialFillExecutor()
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
    )
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    expected_quantity = executor.request.quantity * executor.fraction
    expected_notional = expected_quantity * (
        executor.request.reference_price * executor.fill_price_multiplier
    )
    assert trade.state == TradeLifecycleState.POSITION_OPENED
    assert trade.quantity == pytest.approx(expected_quantity)
    assert trade.position_size_usd == pytest.approx(expected_notional)


class _ZeroFillSuccessExecutor(OrderExecutor):
    async def submit_order(self, request):
        return OrderResult(
            order_id="accepted-unfilled-1",
            symbol=request.symbol,
            side=request.side,
            filled_quantity=0.0,
            fill_price=0.0,
            success=True,
        )


@pytest.mark.asyncio
async def test_lifecycle_never_opens_zero_quantity_position_on_unfilled_order(
    event_bus, risk_engine
):
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=_ZeroFillSuccessExecutor(),
    )
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    assert trade.state == TradeLifecycleState.REJECTED
    assert trade.quantity == 0.0
    assert not lifecycle.get_open_trades()
    assert "confirmed fill" in trade.rejection_reason


@pytest.mark.asyncio
async def test_lifecycle_keeps_confirmed_partial_fill_when_adapter_reports_status_error(
    event_bus, risk_engine
):
    executor = _PartialFillExecutor(
        fraction=0.25,
        success=False,
        error="venue order cancelled after partial execution",
    )
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        order_executor=executor,
    )
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    assert trade.state == TradeLifecycleState.POSITION_OPENED
    assert trade.quantity == pytest.approx(executor.request.quantity * 0.25)
    assert trade.position_size_usd == pytest.approx(
        trade.quantity * executor.request.reference_price * 1.001
    )
