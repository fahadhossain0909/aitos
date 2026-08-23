from datetime import datetime, timezone

from aitos.backtest.end_to_end import AITOSReplay
from aitos.data.schema import CanonicalBookEvent, CanonicalTrade


def test_canonical_trade_and_l2_replay_smoke():
    trades = []
    books = []
    states = []
    replay = AITOSReplay(on_trade=trades.append, on_book=books.append, on_book_state=states.append)

    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        CanonicalBookEvent("binance", "futures_um", "BTCUSDT", 1, ts, "buy", 100.0, 2.0),
        CanonicalBookEvent("binance", "futures_um", "BTCUSDT", 2, ts, "sell", 101.0, 3.0),
        CanonicalTrade("binance", "futures_um", "BTCUSDT", "t1", ts, 101.0, 0.5, "buy", False),
    ]

    stats = replay.replay(events)

    assert stats.book_events == 2
    assert stats.trades == 1
    assert len(books) == 2
    assert len(trades) == 1
    assert states[-1].bids[100.0] == 2.0
    assert states[-1].asks[101.0] == 3.0


def test_stale_l2_update_is_not_forwarded():
    forwarded = []
    replay = AITOSReplay(on_book=forwarded.append)
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert replay.feed_book(CanonicalBookEvent("binance", "futures_um", "BTCUSDT", 10, ts, "buy", 100, 1))
    assert not replay.feed_book(CanonicalBookEvent("binance", "futures_um", "BTCUSDT", 9, ts, "buy", 100, 4))
    assert len(forwarded) == 1
    assert replay.stats.stale_book_events == 1
