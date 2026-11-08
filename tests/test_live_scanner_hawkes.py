"""Live scanner to A-Stat Hawkes event-consumer integration tests."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aitos.intelligence.live_scanner import LiveScannerCache
from aitos.models.market import TradeSide, TradeTick
from aitos.statistics.engine import AStatEngine
from aitos.statistics.hawkes import HawkesMLEFit


class RecordingHawkesEngine:
    def __init__(self):
        self.events = []

    def has_hawkes_model(self, symbol):
        return symbol == "BTCUSDT"

    def observe_marked_event(self, symbol, timestamp, mark):
        self.events.append((symbol, timestamp, mark))
        return SimpleNamespace(intensities=(1.0, 1.0))


def trade(trade_id, side, timestamp):
    return TradeTick(
        symbol="BTCUSDT",
        trade_id=trade_id,
        price=100.0,
        quantity=0.25,
        side=side,
        timestamp=timestamp,
    )


def test_live_trade_ingestion_updates_registered_hawkes_model_in_aggressor_order():
    engine = RecordingHawkesEngine()
    cache = LiveScannerCache(None, ["BTCUSDT"], hawkes_engine=engine)
    timestamp = datetime(2026, 11, 8, tzinfo=timezone.utc)

    asyncio.run(cache.accept_live_trade(trade(10, TradeSide.BUY, timestamp)))
    # Exchange feeds can timestamp multiple prints in the same millisecond.
    asyncio.run(cache.accept_live_trade(trade(11, TradeSide.SELL, timestamp)))
    asyncio.run(
        cache.accept_live_trade(
            trade(12, TradeSide.BUY, timestamp + timedelta(milliseconds=1))
        )
    )

    assert [event[2] for event in engine.events] == [1, 0, 1]
    assert engine.events[0][1] < engine.events[1][1] < engine.events[2][1]
    assert cache.snapshot("BTCUSDT").hawkes_event_rejections == 0


def test_live_trade_ingestion_rejects_out_of_order_hawkes_timestamp():
    engine = RecordingHawkesEngine()
    cache = LiveScannerCache(None, ["BTCUSDT"], hawkes_engine=engine)
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)

    asyncio.run(cache.accept_live_trade(trade(10, TradeSide.BUY, now)))
    asyncio.run(
        cache.accept_live_trade(
            trade(11, TradeSide.SELL, now - timedelta(milliseconds=1))
        )
    )

    assert len(engine.events) == 1
    assert cache.snapshot("BTCUSDT").hawkes_event_rejections == 1



def test_live_hawkes_auto_fit_is_opt_in_and_requires_oos_validation(monkeypatch):
    import aitos.intelligence.live_scanner as live_scanner

    calls = []

    def fake_fit(event_times, marks, *, horizon, decay_grid, max_iterations):
        calls.append((len(event_times), len(marks), horizon))
        return HawkesMLEFit(
            baseline_intensities=(1.0, 1.0),
            excitation_matrix=((0.0, 0.0), (0.0, 0.0)),
            decay=1.0,
            branching_ratio=0.0,
            log_likelihood=-10.0,
            iterations=10,
            events=len(event_times),
            horizon=horizon,
            converged=True,
            gradient_norm=0.0,
        )

    monkeypatch.setattr(live_scanner, "fit_marked_hawkes_mle", fake_fit)
    engine = AStatEngine()
    cache = LiveScannerCache(
        None,
        ["BTCUSDT"],
        hawkes_engine=engine,
        hawkes_auto_fit=True,
        hawkes_min_events=100,
        hawkes_min_span_seconds=5.0,
    )
    start = datetime(2026, 11, 8, tzinfo=timezone.utc)

    async def exercise():
        for index in range(100):
            await cache.accept_live_trade(
                trade(
                    index + 1,
                    TradeSide.BUY if index % 2 else TradeSide.SELL,
                    start + timedelta(milliseconds=100 * index),
                )
            )
        tasks = list(cache._hawkes_fit_tasks.values())
        assert tasks
        await asyncio.gather(*tasks)
        state = cache.snapshot("BTCUSDT")
        assert state.hawkes_fit_status == "ready_oos_validated"
        assert state.hawkes_fit_events == 80
        assert engine.has_hawkes_model("BTCUSDT")
        assert calls and calls[0][0] == 80
        assert engine.last_hawkes_timestamp("BTCUSDT") == state.last_hawkes_timestamp
        await cache.shutdown()

    asyncio.run(exercise())


def test_live_hawkes_fit_is_disabled_by_default():
    engine = AStatEngine()
    cache = LiveScannerCache(None, ["BTCUSDT"], hawkes_engine=engine)
    start = datetime(2026, 11, 8, tzinfo=timezone.utc)

    async def exercise():
        for index in range(120):
            await cache.accept_live_trade(
                trade(
                    index + 1,
                    TradeSide.BUY if index % 2 else TradeSide.SELL,
                    start + timedelta(milliseconds=100 * index),
                )
            )
        assert not engine.has_hawkes_model("BTCUSDT")
        assert cache.snapshot("BTCUSDT").hawkes_fit_status == "disabled"

    asyncio.run(exercise())


def test_live_hawkes_accepts_multiple_prints_with_same_exchange_timestamp():
    engine = RecordingHawkesEngine()
    cache = LiveScannerCache(None, ["BTCUSDT"], hawkes_engine=engine)
    timestamp = datetime(2026, 11, 8, tzinfo=timezone.utc)

    async def exercise():
        for trade_id in range(10, 14):
            await cache.accept_live_trade(
                trade(trade_id, TradeSide.BUY, timestamp)
            )

    asyncio.run(exercise())
    assert len(engine.events) == 4
    assert all(
        left[1] < right[1]
        for left, right in zip(engine.events, engine.events[1:])
    )
    assert cache.snapshot("BTCUSDT").hawkes_event_rejections == 0
