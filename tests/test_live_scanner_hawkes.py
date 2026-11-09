"""Live scanner to A-Stat Hawkes event-consumer integration tests."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aitos.intelligence.live_scanner import LiveScannerCache
from aitos.models.market import TradeSide, TradeTick


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
