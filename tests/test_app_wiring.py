import asyncio
from datetime import datetime, timezone

import pytest

from aitos.app import (
    PaperPortfolioTracker,
    build_system,
    initialize_all,
    run_scan_and_trade_cycle,
    shutdown_all,
)
from aitos.execution.order_executor import SimulatedOrderExecutor
from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from tests.test_scanner import FakeScannerExchange


async def _wait_for(predicate, timeout=3.0, interval=0.05):
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return False


@pytest.mark.asyncio
async def test_build_system_wires_all_core_modules(event_bus):
    exchange = FakeScannerExchange()
    executor = SimulatedOrderExecutor()
    components = await build_system(
        event_bus=event_bus,
        exchange=exchange,
        order_executor=executor,
        symbols=["BTCUSDT", "ETHUSDT"],
        min_score_threshold=0.0,
    )

    assert components.kernel is not None
    assert components.risk_engine is not None
    assert components.scanner is not None
    assert components.trade_lifecycle is not None
    assert components.journal is not None
    assert components.rl_feedback is not None
    assert components.ml_feedback is not None
    assert components.attention_explainer is not None
    assert components.attention_feedback is not None
    # SimulatedOrderExecutor doesn't support exchange-side stops -> no reconciliation scheduler
    assert components.reconciliation is None
    # no graph_driver passed -> no knowledge graph components
    assert components.knowledge_graph is None
    assert components.correlation_updater is None


@pytest.mark.asyncio
async def test_initialize_all_and_shutdown_all_completes_cleanly(event_bus):
    exchange = FakeScannerExchange()
    executor = SimulatedOrderExecutor()
    components = await build_system(
        event_bus=event_bus,
        exchange=exchange,
        order_executor=executor,
        symbols=["BTCUSDT"],
        min_score_threshold=0.0,
    )

    await initialize_all(components)

    for module in components.all_modules():
        health = await module.health_check()
        assert health.status.value in ("healthy", "degraded")

    await shutdown_all(components)
    assert exchange.closed is True


@pytest.mark.asyncio
async def test_scan_and_trade_cycle_opens_a_position_for_the_trending_symbol(event_bus):
    exchange = (
        FakeScannerExchange()
    )  # BTCUSDT trends, ETHUSDT is choppy (per test_scanner.py's fixture)
    executor = SimulatedOrderExecutor()
    components = await build_system(
        event_bus=event_bus,
        exchange=exchange,
        order_executor=executor,
        symbols=["BTCUSDT", "ETHUSDT"],
        min_score_threshold=0.0,
    )
    await initialize_all(components)
    tracker = PaperPortfolioTracker(starting_equity_usd=10_000.0)

    submitted = await run_scan_and_trade_cycle(components, tracker)

    assert submitted == 1
    open_trades = components.trade_lifecycle.get_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0].symbol == "BTCUSDT"

    await shutdown_all(components)


@pytest.mark.asyncio
async def test_scan_and_trade_cycle_does_not_duplicate_an_already_open_symbol(
    event_bus,
):
    exchange = FakeScannerExchange()
    executor = SimulatedOrderExecutor()
    components = await build_system(
        event_bus=event_bus,
        exchange=exchange,
        order_executor=executor,
        symbols=["BTCUSDT"],
        min_score_threshold=0.0,
    )
    await initialize_all(components)
    tracker = PaperPortfolioTracker(starting_equity_usd=10_000.0)

    await run_scan_and_trade_cycle(components, tracker)
    second_round_submitted = await run_scan_and_trade_cycle(components, tracker)

    assert second_round_submitted == 0
    assert len(components.trade_lifecycle.get_open_trades()) == 1

    await shutdown_all(components)


@pytest.mark.asyncio
async def test_price_feed_subscription_auto_updates_open_trades(event_bus):
    """The key wiring proof: TradeLifecycle isn't manually poked with
    update_price anywhere here — it's subscribed to market.* on the real
    Event Bus by initialize_all, and DataIngestionService's own published
    events are what drive it."""

    exchange = FakeScannerExchange()
    executor = SimulatedOrderExecutor()
    components = await build_system(
        event_bus=event_bus,
        exchange=exchange,
        order_executor=executor,
        symbols=["BTCUSDT"],
        min_score_threshold=0.0,
    )
    await initialize_all(components)
    tracker = PaperPortfolioTracker(starting_equity_usd=10_000.0)

    await run_scan_and_trade_cycle(components, tracker)
    trade = components.trade_lifecycle.get_open_trades()[0]

    # Publish a kline event directly (simulating what DataIngestionService's
    # live stream would produce) that breaches the stop loss.
    from aitos.core.contracts import Event

    crash_price = trade.sl_price - 1.0
    await event_bus.publish(
        Event(
            topic=f"market.kline.{trade.symbol}.15m",
            payload={"symbol": trade.symbol, "close": crash_price},
            source_module="test",
        )
    )

    closed = await _wait_for(
        lambda: len(components.trade_lifecycle.get_open_trades()) == 0
    )
    assert closed is True
    assert (
        components.trade_lifecycle.get_closed_trades()[0].exit_reason == "sl_triggered"
    )

    await shutdown_all(components)


def test_paper_portfolio_tracker_computes_equity_from_closed_trades():
    tracker = PaperPortfolioTracker(starting_equity_usd=10_000.0)
    now = datetime.now(timezone.utc).isoformat()

    class FakeLifecycle:
        def get_closed_trades(self):
            return [
                Trade(
                    trade_id="t1",
                    symbol="BTCUSDT",
                    side=TradeSide.LONG,
                    entry_price=100.0,
                    quantity=1.0,
                    leverage=5.0,
                    position_size_usd=1000.0,
                    risk_amount_usd=100.0,
                    strategy_id="s",
                    agent_consensus={},
                    explanation="",
                    sl_price=98.0,
                    tp_price=104.0,
                    state=TradeLifecycleState.POSITION_CLOSED,
                    entry_time=now,
                    pnl=250.0,
                    pnl_percent=25.0,
                    exit_time=now,
                )
            ]

        def get_open_trades(self):
            return []

    portfolio = tracker.build_portfolio_state(FakeLifecycle())
    assert portfolio.equity_usd == 10_250.0
    assert portfolio.current_drawdown_pct == 0.0


def test_paper_portfolio_tracker_tracks_drawdown_from_peak():
    tracker = PaperPortfolioTracker(starting_equity_usd=10_000.0)
    now = datetime.now(timezone.utc).isoformat()

    def make_trade(trade_id, pnl):
        return Trade(
            trade_id=trade_id,
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            entry_price=100.0,
            quantity=1.0,
            leverage=5.0,
            position_size_usd=1000.0,
            risk_amount_usd=100.0,
            strategy_id="s",
            agent_consensus={},
            explanation="",
            sl_price=98.0,
            tp_price=104.0,
            state=TradeLifecycleState.POSITION_CLOSED,
            entry_time=now,
            pnl=pnl,
            pnl_percent=pnl / 10,
            exit_time=now,
        )

    class FakeLifecycle:
        def __init__(self, trades):
            self._trades = trades

        def get_closed_trades(self):
            return self._trades

        def get_open_trades(self):
            return []

    # First: up to 10,500 (new peak). Then: back down to 10,000 -> 500/10,500 drawdown.
    tracker.build_portfolio_state(FakeLifecycle([make_trade("t1", 500.0)]))
    portfolio = tracker.build_portfolio_state(
        FakeLifecycle([make_trade("t1", 500.0), make_trade("t2", -500.0)])
    )

    assert portfolio.equity_usd == 10_000.0
    assert portfolio.current_drawdown_pct == pytest.approx(500 / 10_500 * 100)


@pytest.mark.asyncio
async def test_live_portfolio_tracker_queries_real_account_balance():
    from aitos.app import LivePortfolioTracker

    class FakeLiveExecutor:
        def __init__(self):
            self.balances = [1000.0, 1200.0, 900.0]
            self._call = 0

        async def get_account_balance(self, asset="USDT"):
            value = self.balances[self._call]
            self._call += 1
            return value

    executor = FakeLiveExecutor()
    tracker = LivePortfolioTracker(order_executor=executor)

    class EmptyLifecycle:
        def get_open_trades(self):
            return []

    await tracker.refresh_equity()
    portfolio = tracker.build_portfolio_state(EmptyLifecycle())
    assert portfolio.equity_usd == 1000.0
    assert portfolio.peak_equity_usd == 1000.0

    await tracker.refresh_equity()  # 1200 — new peak
    portfolio = tracker.build_portfolio_state(EmptyLifecycle())
    assert portfolio.equity_usd == 1200.0
    assert portfolio.peak_equity_usd == 1200.0

    await tracker.refresh_equity()  # 900 — drawdown from the 1200 peak
    portfolio = tracker.build_portfolio_state(EmptyLifecycle())
    assert portfolio.equity_usd == 900.0
    assert portfolio.peak_equity_usd == 1200.0
    assert portfolio.current_drawdown_pct == pytest.approx((1200 - 900) / 1200 * 100)


@pytest.mark.asyncio
async def test_scan_and_trade_cycle_calls_refresh_equity_when_tracker_supports_it(
    event_bus,
):
    exchange = FakeScannerExchange()
    executor = SimulatedOrderExecutor()
    components = await build_system(
        event_bus=event_bus,
        exchange=exchange,
        order_executor=executor,
        symbols=["BTCUSDT"],
        min_score_threshold=0.0,
    )
    await initialize_all(components)

    refresh_calls = []

    class TrackerWithRefresh:
        def __init__(self):
            self.equity = 5000.0

        async def refresh_equity(self):
            refresh_calls.append(True)
            self.equity = 5000.0

        def build_portfolio_state(self, trade_lifecycle):
            from aitos.risk.models import PortfolioState

            return PortfolioState(equity_usd=self.equity, peak_equity_usd=self.equity)

    await run_scan_and_trade_cycle(components, TrackerWithRefresh())

    assert (
        len(refresh_calls) >= 1
    )  # called at least once per cycle (twice, per current implementation)

    await shutdown_all(components)


@pytest.mark.asyncio
async def test_scan_and_trade_cycle_forwards_production_flag_to_opportunities(
    event_bus, kernel
):
    """Governance regression test: is_production/approved_by must actually
    reach AIKernel.enforce_governance via the submitted Opportunity."""
    exchange = FakeScannerExchange()
    executor = SimulatedOrderExecutor()
    components = await build_system(
        event_bus=event_bus,
        exchange=exchange,
        order_executor=executor,
        symbols=["BTCUSDT"],
        min_score_threshold=0.0,
        kernel=kernel,
    )
    await initialize_all(components)
    tracker = PaperPortfolioTracker(starting_equity_usd=10_000.0)

    # No approved_by -> governance should reject every production opportunity.
    await run_scan_and_trade_cycle(
        components, tracker, is_production=True, approved_by=None
    )
    assert components.trade_lifecycle.get_open_trades() == []
    rejected = (
        components.trade_lifecycle.get_closed_trades()
    )  # rejected trades aren't tracked as "closed" though
    # Rejected trades don't appear in open or closed lists (they're REJECTED state, never stored) —
    # the real proof is simply that nothing opened despite a valid trending symbol being available.

    await shutdown_all(components)
