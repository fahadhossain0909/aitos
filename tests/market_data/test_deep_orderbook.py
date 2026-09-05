from datetime import datetime, timezone

from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource
from aitos.market_data.deep_orderbook import (
    DEEP_SYMBOLS,
    DeepOrderBookGap,
    DeepOrderBookStore,
)


def _event(event_type, symbol="BTCUSDT", payload=None):
    return MarketEvent(
        event_type=event_type,
        exchange="binance",
        market="usd_m_futures",
        symbol=symbol,
        event_time=datetime(2026, 9, 4, tzinfo=timezone.utc),
        payload=payload or {},
        source=MarketSource.WEBSOCKET,
    )


def test_deep_symbols_are_exactly_btc_and_ltc():
    assert DEEP_SYMBOLS == {"BTCUSDT", "LTCUSDT"}


def test_store_rejects_non_deep_symbol_without_write():
    class Repo:
        _client = object()

    store = DeepOrderBookStore(Repo())

    import asyncio

    asyncio.run(
        store.persist(
            _event(
                MarketEventType.BOOK_DELTA,
                symbol="ETHUSDT",
                payload={
                    "first_update_id": 1,
                    "final_update_id": 1,
                    "bids": [],
                    "asks": [],
                },
            )
        )
    )
    assert store.rejected_symbols == 1


def test_replay_gap_type_is_explicit():
    assert issubclass(DeepOrderBookGap, RuntimeError)
