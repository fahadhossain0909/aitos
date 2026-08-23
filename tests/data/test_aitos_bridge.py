from datetime import datetime, timezone

from aitos.data.aitos_bridge import (book_events_to_snapshot,
                                     canonical_trade_to_domain)
from aitos.data.schema import CanonicalBookEvent, CanonicalTrade
from aitos.models.market import TradeSide


def test_canonical_trade_maps_to_existing_trade_tick():
    event = CanonicalTrade(
        "binance",
        "futures_um",
        "BTCUSDT",
        "42",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        100.0,
        2.5,
        "sell",
        True,
    )
    tick = canonical_trade_to_domain(event)
    assert tick.trade_id == 42
    assert tick.side is TradeSide.SELL
    assert tick.is_buyer_maker is True
    assert tick.quantity == 2.5


def test_book_events_map_to_sorted_snapshot_and_remove_zero_level():
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        CanonicalBookEvent("binance", "futures_um", "BTCUSDT", 1, ts, "buy", 99.0, 2.0),
        CanonicalBookEvent(
            "binance", "futures_um", "BTCUSDT", 1, ts, "sell", 101.0, 3.0
        ),
        CanonicalBookEvent("binance", "futures_um", "BTCUSDT", 2, ts, "buy", 99.0, 0.0),
    ]
    snapshot = book_events_to_snapshot(events)
    assert snapshot is not None
    assert snapshot.bids == ()
    assert snapshot.asks == ((101.0, 3.0),)
    assert snapshot.last_update_id == 2
