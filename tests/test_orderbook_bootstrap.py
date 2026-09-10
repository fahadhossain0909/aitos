from datetime import datetime, timezone

from aitos.exchange.orderbook import DepthUpdate, LocalOrderBook
from aitos.models.market import OrderBookSnapshot


def _snapshot(update_id: int = 100) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        symbol="BTCUSDT",
        bids=((100.0, 1.0),),
        asks=((101.0, 1.0),),
        last_update_id=update_id,
        timestamp=datetime.now(timezone.utc),
    )


def _update(first: int, final: int, previous: int = 0) -> DepthUpdate:
    return DepthUpdate(
        first_update_id=first,
        final_update_id=final,
        previous_update_id=previous,
        bids=((100.0, 2.0),),
        asks=(),
        event_time_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
    )


def test_bootstrap_discards_diffs_already_covered_by_rest_snapshot():
    book = LocalOrderBook("BTCUSDT", max_levels=20)
    book.seed(_snapshot(100))

    result = book.apply(_update(95, 100))

    assert result is None
    assert book.last_update_id == 100
    assert book.forensics_snapshot()["stale_updates"] == 1
    assert book.forensics_snapshot()["awaiting_first_update"] is True


def test_bootstrap_accepts_first_diff_that_bridges_rest_snapshot():
    book = LocalOrderBook("BTCUSDT", max_levels=20)
    book.seed(_snapshot(100))

    assert book.apply(_update(95, 100)) is None
    result = book.apply(_update(101, 102))

    assert result is not None
    assert result.last_update_id == 102
    assert result.bids == ((100.0, 2.0),)
    assert book.forensics_snapshot()["awaiting_first_update"] is False
