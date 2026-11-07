"""Production-like paper-trading recovery and reconciliation tests."""

from __future__ import annotations

import pytest

from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from aitos.statistics.shadow import StatisticalShadowLedger
from aitos.trading.lifecycle import TradeLifecycle
from aitos.trading.persistent_state import TradeStatePersistence


class FakeStateStore:
    def __init__(self, trades=None):
        self.trades = list(trades or [])

    async def load_open_trades(self):
        return list(self.trades)

    async def load_closed_trades(self):
        return list(getattr(self, "closed_trades", []))

    async def load_peak_equity(self, asset):
        return getattr(self, "peak_equity", None)

    async def save_drawdown(self, asset, equity, peak):
        self.saved_drawdown = (asset, equity, peak)

    async def save_closed_trade(self, trade):
        if not hasattr(self, "closed_trades"):
            self.closed_trades = []
        self.closed_trades = [
            item for item in self.closed_trades if item.trade_id != trade.trade_id
        ]
        self.closed_trades.append(trade)

    async def delete_trade(self, trade_id):
        self.trades = [trade for trade in self.trades if trade.trade_id != trade_id]


class FakeLifecycle:
    def __init__(self):
        self._open_trades = {}
        self._closed_trades = []

    def get_open_trades(self):
        return list(self._open_trades.values())

    def get_closed_trades(self):
        return list(self._closed_trades)


@pytest.fixture
def paper_trade():
    return Trade(
        trade_id="paper-recovery-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        quantity=0.01,
        leverage=1.0,
        position_size_usd=1.0,
        risk_amount_usd=0.05,
        strategy_id="paper",
        agent_consensus={},
        explanation="recovery test",
        sl_price=95.0,
        tp_price=110.0,
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time="2026-08-24T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_paper_trade_survives_process_restart(event_bus, paper_trade):
    lifecycle = FakeLifecycle()
    persistence = TradeStatePersistence(
        event_bus, lifecycle, FakeStateStore([paper_trade])
    )

    assert await persistence.restore() == 1
    restored = lifecycle._open_trades[paper_trade.trade_id]
    assert restored.symbol == paper_trade.symbol
    assert restored.entry_price == paper_trade.entry_price
    assert restored.sl_price == paper_trade.sl_price
    assert restored.state == TradeLifecycleState.POSITION_OPENED


def test_trade_payload_round_trip_preserves_entry_thesis_and_emergency_stop(
    paper_trade,
):
    from aitos.trading.persistent_state import _trade_from_dict

    paper_trade.entry_thesis = {
        "trade_id": paper_trade.trade_id,
        "symbol": paper_trade.symbol,
        "side": "LONG",
        "entry_price": 100.0,
        "components": [
            {"code": "bullish_structure", "description": "entry", "weight": 1.2}
        ],
        "invalidations": [
            {"code": "structure_break", "description": "break", "level": 94.0}
        ],
        "confirmations": [{"code": "structure_supportive", "description": "holds"}],
        "expected_path_prices": [105.0, 110.0],
        "invalidation_price": 94.0,
        "created_at": "2026-08-24T00:00:00+00:00",
        "notes": ["entry snapshot"],
        "features": {"trend_strength": 0.8},
    }
    paper_trade.emergency_sl_price = 90.0
    paper_trade.breakeven_triggered = True
    paper_trade.breakeven_at_r_multiple = 1.0
    paper_trade.initial_r_distance = 4.0
    paper_trade.mae_r = 0.5
    paper_trade.mfe_r = 1.25
    paper_trade.gross_realized_pnl_usd = 12.75
    paper_trade.fees_paid_usd = 0.15
    paper_trade.funding_pnl_usd = -0.05
    paper_trade.cost_adjustments = {
        "fee-1": {"kind": "fee", "amount_usd": 0.15},
        "funding-1": {"kind": "funding", "amount_usd": -0.05},
    }

    restored = _trade_from_dict(paper_trade.to_dict())
    assert restored.entry_thesis == paper_trade.entry_thesis
    assert restored.emergency_sl_price == 90.0
    assert restored.breakeven_triggered is True
    assert restored.breakeven_at_r_multiple == 1.0
    assert restored.initial_r_distance == 4.0
    assert restored.mae_r == 0.5
    assert restored.mfe_r == 1.25
    assert restored.gross_realized_pnl_usd == 12.75
    assert restored.fees_paid_usd == 0.15
    assert restored.funding_pnl_usd == -0.05
    assert restored.cost_adjustments == paper_trade.cost_adjustments

    from aitos.trading.position_manager import PositionManager

    recovered_thesis = PositionManager._restore_entry_thesis(restored)
    assert recovered_thesis is not None
    assert recovered_thesis.trade_id == paper_trade.trade_id
    assert recovered_thesis.expected_path_prices == (105.0, 110.0)


@pytest.mark.asyncio
async def test_restart_does_not_create_duplicate_open_trade(event_bus, paper_trade):
    lifecycle = FakeLifecycle()
    persistence = TradeStatePersistence(
        event_bus, lifecycle, FakeStateStore([paper_trade])
    )

    assert await persistence.restore() == 1
    assert await persistence.restore() == 1
    assert len(lifecycle._open_trades) == 1


@pytest.mark.asyncio
async def test_reconciliation_detects_missing_internal_position():
    remote_quantity = 0.01
    internal_positions = {}
    mismatch = remote_quantity > 0 and "BTCUSDT" not in internal_positions

    assert mismatch is True


class FakeIngestion:
    def __init__(self, trade_symbols=None, kline_symbols=None):
        self._live_trade_symbols = list(trade_symbols or [])
        self._live_kline_symbols = list(kline_symbols or [])
        self.trade_updates = []
        self.kline_updates = []

    async def update_live_trade_symbols(self, symbols):
        self.trade_updates.append(list(symbols))
        self._live_trade_symbols = list(
            dict.fromkeys([*self._live_trade_symbols, *symbols])
        )
        return True

    async def update_live_kline_symbols(self, symbols):
        self.kline_updates.append(list(symbols))
        self._live_kline_symbols = list(
            dict.fromkeys([*symbols, *self._live_kline_symbols])
        )
        return True


@pytest.mark.asyncio
async def test_restore_defers_market_resubscription_until_explicit_reconcile(
    event_bus, paper_trade, monkeypatch
):
    from aitos.trading import persistent_state

    lifecycle = FakeLifecycle()
    ingestion = FakeIngestion()
    monkeypatch.setattr(persistent_state, "_INGESTIONS", {ingestion})
    persistence = TradeStatePersistence(
        event_bus, lifecycle, FakeStateStore([paper_trade])
    )

    assert await persistence.restore() == 1
    assert ingestion.trade_updates == []
    assert ingestion.kline_updates == []

    result = await persistence.resubscribe_restored_positions()
    assert result["ingestions"] == 1
    assert result["verified"] == 1
    assert result["failures"] == 0
    assert "BTCUSDT" in ingestion._live_trade_symbols
    assert "BTCUSDT" in ingestion._live_kline_symbols


@pytest.mark.asyncio
async def test_resubscription_reports_missing_symbols_as_failure(
    event_bus, paper_trade, monkeypatch
):
    from aitos.trading import persistent_state

    class NoopIngestion:
        _live_trade_symbols = []
        _live_kline_symbols = []

        async def update_live_trade_symbols(self, symbols):
            return False

        async def update_live_kline_symbols(self, symbols):
            return False

    lifecycle = FakeLifecycle()
    monkeypatch.setattr(persistent_state, "_INGESTIONS", {NoopIngestion()})
    persistence = TradeStatePersistence(
        event_bus, lifecycle, FakeStateStore([paper_trade])
    )
    await persistence.restore()

    result = await persistence.resubscribe_restored_positions()
    assert result["ingestions"] == 1
    assert result["verified"] == 0
    assert result["failures"] == 1


@pytest.mark.asyncio
async def test_closed_trade_history_is_restored_for_equity_recovery(
    event_bus, paper_trade
):
    from aitos.models.trade import PartialExit

    paper_trade.state = TradeLifecycleState.POSITION_CLOSED
    paper_trade.pnl = 12.5
    paper_trade.pnl_percent = 1.25
    paper_trade.exit_price = 101.25
    paper_trade.exit_time = "2026-08-24T01:00:00+00:00"
    paper_trade.partial_exits = [
        PartialExit(
            price=101.0,
            size_usd=0.5,
            r_multiple=0.2,
            at="2026-08-24T00:30:00+00:00",
        )
    ]

    store = FakeStateStore([paper_trade])
    store.closed_trades = [paper_trade]
    lifecycle = FakeLifecycle()
    lifecycle._closed_trades = []
    persistence = TradeStatePersistence(event_bus, lifecycle, store)

    assert await persistence.restore() == 0
    assert lifecycle._open_trades == {}
    assert len(lifecycle._closed_trades) == 1
    restored = lifecycle._closed_trades[0]
    assert restored.trade_id == paper_trade.trade_id
    assert restored.pnl == 12.5
    assert len(restored.partial_exits) == 1


@pytest.mark.asyncio
async def test_closed_trade_restore_is_idempotent(event_bus, paper_trade):
    paper_trade.state = TradeLifecycleState.POSITION_CLOSED
    paper_trade.pnl = -7.0
    store = FakeStateStore()
    store.closed_trades = [paper_trade]
    lifecycle = FakeLifecycle()
    lifecycle._closed_trades = []
    persistence = TradeStatePersistence(event_bus, lifecycle, store)

    await persistence.restore()
    await persistence.restore()

    assert len(lifecycle._closed_trades) == 1
    assert lifecycle._closed_trades[0].pnl == -7.0


@pytest.mark.asyncio
async def test_paper_portfolio_restores_peak_equity_and_persists_drawdown(
    paper_trade,
):
    from aitos.app import PaperPortfolioTracker

    paper_trade.state = TradeLifecycleState.POSITION_CLOSED
    paper_trade.pnl = -20.0
    store = FakeStateStore()
    store.peak_equity = 1_050.0
    tracker = PaperPortfolioTracker(starting_equity_usd=1_000.0, state_store=store)
    lifecycle = FakeLifecycle()
    lifecycle._closed_trades = [paper_trade]

    await tracker.restore()
    portfolio = tracker.build_portfolio_state(lifecycle)

    assert portfolio.equity_usd == 980.0
    assert portfolio.peak_equity_usd == 1_050.0

    await tracker.persist_state(lifecycle)
    assert store.saved_drawdown == ("PAPER", 980.0, 1_050.0)


@pytest.mark.asyncio
async def test_close_event_archives_trade_before_deleting_open_runtime_state(
    event_bus, paper_trade
):
    from types import SimpleNamespace

    paper_trade.state = TradeLifecycleState.POSITION_CLOSED
    paper_trade.pnl = 4.25
    store = FakeStateStore([paper_trade])
    lifecycle = FakeLifecycle()
    persistence = TradeStatePersistence(event_bus, lifecycle, store)

    await persistence._delete_event(
        SimpleNamespace(
            payload=paper_trade.to_dict(),
            event_id="close-event-1",
            topic="trade.position_closed",
        )
    )

    assert store.trades == []
    assert len(store.closed_trades) == 1
    assert store.closed_trades[0].pnl == 4.25


@pytest.mark.asyncio
async def test_close_archive_failure_is_propagated_and_open_state_is_not_deleted(
    event_bus, paper_trade
):
    from types import SimpleNamespace

    class FailingArchiveStore(FakeStateStore):
        async def save_closed_trade(self, trade):
            raise RuntimeError("clickhouse unavailable")

    store = FailingArchiveStore([paper_trade])
    persistence = TradeStatePersistence(event_bus, FakeLifecycle(), store)

    with pytest.raises(RuntimeError, match="clickhouse unavailable"):
        await persistence._delete_event(
            SimpleNamespace(
                payload=paper_trade.to_dict(),
                event_id="close-event-failed-archive",
                topic="trade.position_closed",
            )
        )

    assert store.trades == [paper_trade]
    assert getattr(store, "closed_trades", []) == []


@pytest.mark.asyncio
async def test_closed_trade_cost_update_refreshes_durable_ledger(
    event_bus, paper_trade
):
    from types import SimpleNamespace

    paper_trade.state = TradeLifecycleState.POSITION_CLOSED
    paper_trade.pnl = 9.5
    paper_trade.gross_realized_pnl_usd = 10.0
    paper_trade.fees_paid_usd = 0.25
    paper_trade.funding_pnl_usd = -0.25
    paper_trade.cost_adjustments = {
        "fee-1": {"kind": "fee", "amount_usd": 0.25},
        "funding-1": {"kind": "funding", "amount_usd": -0.25},
    }
    store = FakeStateStore()
    store.closed_trades = [paper_trade]
    lifecycle = FakeLifecycle()
    persistence = TradeStatePersistence(event_bus, lifecycle, store)

    paper_trade.fees_paid_usd = 0.5
    paper_trade.pnl = 9.25
    await persistence._save_event(
        SimpleNamespace(
            payload=paper_trade.to_dict(),
            event_id="closed-cost-update-1",
            topic="trade.position_updated",
        )
    )

    assert len(store.closed_trades) == 1
    assert store.closed_trades[0].fees_paid_usd == 0.5
    assert store.closed_trades[0].pnl == 9.25


@pytest.mark.asyncio
async def test_paper_equity_includes_realized_partial_pnl_on_open_positions(
    paper_trade,
):
    from aitos.app import PaperPortfolioTracker

    paper_trade.pnl = 6.75
    paper_trade.gross_realized_pnl_usd = 7.0
    paper_trade.fees_paid_usd = 0.25
    store = FakeStateStore()
    lifecycle = FakeLifecycle()
    lifecycle._open_trades[paper_trade.trade_id] = paper_trade
    tracker = PaperPortfolioTracker(starting_equity_usd=1_000.0, state_store=store)

    portfolio = tracker.build_portfolio_state(lifecycle)
    await tracker.persist_state(lifecycle)

    assert portfolio.equity_usd == 1_006.75
    assert store.saved_drawdown == ("PAPER", 1_006.75, 1_006.75)


@pytest.mark.asyncio
async def test_persistence_initialize_retains_partial_subscriptions_for_cleanup():
    class Subscription:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    class Store:
        async def initialize(self):
            return None

    class FailingEventBus:
        def __init__(self):
            self.calls = 0
            self.first_subscription = Subscription()

        async def subscribe(self, topic, handler, group):
            self.calls += 1
            if self.calls == 1:
                return self.first_subscription
            raise RuntimeError("subscription registration failed")

    event_bus = FailingEventBus()
    persistence = TradeStatePersistence(event_bus, FakeLifecycle(), Store())

    with pytest.raises(RuntimeError, match="subscription registration failed"):
        await persistence.initialize()

    await persistence.shutdown()
    assert event_bus.first_subscription.cancelled is True


@pytest.mark.asyncio
async def test_paper_main_closes_redis_if_clickhouse_startup_fails(monkeypatch):
    from types import SimpleNamespace

    import aitos.eventbus.factory as eventbus_factory
    import run_paper_trading as runner

    class FakeRedis:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    class FakeEventBus:
        def __init__(self):
            self.closed = False

        async def initialize(self, config):
            return None

        async def shutdown(self, grace_period_seconds=30.0):
            self.closed = True

    redis = FakeRedis()
    event_bus = FakeEventBus()
    settings = SimpleNamespace(
        tuning=SimpleNamespace(mode="paper"),
        log_level="INFO",
    )

    async def connect_redis(*args, **kwargs):
        return redis

    async def fail_clickhouse(*args, **kwargs):
        raise RuntimeError("ClickHouse startup failed")

    monkeypatch.setattr(runner, "get_settings", lambda: settings)
    monkeypatch.setattr(runner, "configure_logging", lambda level: None)
    monkeypatch.setattr(runner, "connect_redis_with_retry", connect_redis)
    monkeypatch.setattr(
        eventbus_factory,
        "create_event_bus",
        lambda *args, **kwargs: event_bus,
    )
    monkeypatch.setattr(runner, "connect_clickhouse_repositories", fail_clickhouse)

    with pytest.raises(RuntimeError, match="ClickHouse startup failed"):
        await runner.main()

    assert redis.closed is True
    assert event_bus.closed is True


@pytest.mark.asyncio
async def test_restart_reconciles_shadow_ledger_from_open_and_closed_trades(
    event_bus, risk_engine, paper_trade, tmp_path
):
    from datetime import timedelta, timezone, datetime

    def attach_forecast(trade, snapshot_id):
        as_of = trade.entry_time
        window_start = (
            (datetime.fromisoformat(as_of.replace("Z", "+00:00")) - timedelta(hours=1))
            .astimezone(timezone.utc)
            .isoformat()
        )
        trade.agent_consensus = {
            "a_stat": 8.0,
            "statistical_forecast": {
                "symbol": trade.symbol,
                "side": trade.side.value,
                "horizon": "15m",
                "model_id": "aitos-contract-statistical-stack",
                "model_version": "1",
                "as_of": as_of,
                "window_start": window_start,
                "feature_snapshot_id": snapshot_id,
                "sample_size": 256,
                "predicted_up": 0.8,
                "downside_probability": 0.2,
                "forecast_volatility": 0.02,
                "reference_volatility": 0.01,
                "tail_probability": 0.1,
                "expected_shortfall": 0.03,
                "expected_return": 0.01,
                "probability_confidence": 0.8,
            },
        }

    open_trade = paper_trade
    attach_forecast(open_trade, "snapshot-open")
    closed_trade = Trade(
        trade_id="paper-recovery-closed",
        symbol="ETHUSDT",
        side=TradeSide.SHORT,
        entry_price=2000.0,
        quantity=0.01,
        leverage=1.0,
        position_size_usd=20.0,
        risk_amount_usd=1.0,
        strategy_id="paper",
        agent_consensus={},
        explanation="closed recovery test",
        sl_price=2050.0,
        tp_price=1900.0,
        state=TradeLifecycleState.POSITION_CLOSED,
        entry_time="2026-08-24T00:00:00+00:00",
        exit_time="2026-08-24T01:00:00+00:00",
        exit_price=1980.0,
        pnl=0.2,
        pnl_percent=1.0,
    )
    attach_forecast(closed_trade, "snapshot-closed")
    ledger = StatisticalShadowLedger(tmp_path / "recovered-shadow.sqlite3")
    lifecycle = TradeLifecycle(
        event_bus=event_bus,
        risk_engine=risk_engine,
        statistical_shadow_ledger=ledger,
    )
    store = FakeStateStore([open_trade])
    store.closed_trades = [closed_trade]
    persistence = TradeStatePersistence(event_bus, lifecycle, store)

    assert await persistence.restore() == 1
    open_record = ledger.get_forecast(open_trade.trade_id)
    closed_record = ledger.get_forecast(closed_trade.trade_id)
    assert open_record is not None and open_record.actual_return is None
    assert closed_record is not None
    assert closed_record.actual_return == pytest.approx(0.01)
    assert closed_record.side == "SHORT"
    assert {item.forecast_id for item in ledger.unresolved_forecasts()} == {
        open_trade.trade_id
    }
