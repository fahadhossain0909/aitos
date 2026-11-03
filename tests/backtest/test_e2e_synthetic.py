from datetime import datetime, timedelta, timezone

from aitos.backtest.market_adapter import HistoricalMarketAdapter
from aitos.models.market import OrderBookSnapshot, TradeTick


def test_historical_market_adapter_replays_synthetic_events():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    adapter = HistoricalMarketAdapter("BTCUSDT", tick_size=1.0, trade_window=10)
    events = [
        OrderBookSnapshot(
            symbol="BTCUSDT",
            bids=[{"price": 100.0, "quantity": 2.0}],
            asks=[{"price": 101.0, "quantity": 2.0}],
            last_update_id=1,
            timestamp=base,
        ),
        TradeTick(
            trade_id="t1",
            symbol="BTCUSDT",
            price=101.0,
            quantity=1.0,
            side="buy",
            timestamp=base + timedelta(seconds=1),
            is_buyer_maker=False,
        ),
        OrderBookSnapshot(
            symbol="BTCUSDT",
            bids=[{"price": 100.0, "quantity": 1.0}],
            asks=[{"price": 101.0, "quantity": 2.0}],
            last_update_id=2,
            timestamp=base + timedelta(seconds=2),
        ),
    ]
    state = adapter.feed(events)
    assert state.symbol == "BTCUSDT"
    assert state.latest_trade is not None
    assert state.latest_order_book is not None
    assert state.latest_trade.trade_id == "t1"
