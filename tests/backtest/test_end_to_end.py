from datetime import datetime, timezone

from aitos.backtest.end_to_end import AITOSReplay
from aitos.data.schema import CanonicalBookEvent, CanonicalTrade


def test_replay_routes_trade_and_book_events():
    seen = []
    runner = AITOSReplay(on_trade=lambda e: seen.append(("trade", e.trade_id)), on_book=lambda e: seen.append(("book", e.update_id)))
    events = [
        CanonicalTrade("binance", "futures_um", "BTCUSDT", "1", datetime.now(timezone.utc), 100.0, 0.1, "buy", False),
        CanonicalBookEvent("binance", "futures_um", "BTCUSDT", 1, datetime.now(timezone.utc), "buy", 99.0, 2.0),
    ]
    stats = runner.replay(events)
    assert stats.trades == 1
    assert stats.book_events == 1
    assert seen == [("trade", "1"), ("book", 1)]


def test_stale_book_update_is_not_forwarded():
    seen = []
    runner = AITOSReplay(on_book=lambda e: seen.append(e.update_id))
    ts = datetime.now(timezone.utc)
    runner.feed_book(CanonicalBookEvent("binance", "futures_um", "BTCUSDT", 10, ts, "buy", 99, 1))
    assert not runner.feed_book(CanonicalBookEvent("binance", "futures_um", "BTCUSDT", 9, ts, "buy", 99, 2))
    assert seen == [10]
    assert runner.stats.stale_book_events == 1
