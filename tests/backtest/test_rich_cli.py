from types import SimpleNamespace

from aitos.backtest.rich_cli import _row_to_event, auction_decision
from aitos.models.market import OrderBookSnapshot, TradeTick


def test_auction_baseline_returns_flat_below_threshold():
    decision = auction_decision(
        SimpleNamespace(auction_long_score=0.4, auction_short_score=0.5)
    )
    assert decision.direction == "flat"
    assert decision.quantity == 0


def test_auction_baseline_selects_stronger_side():
    decision = auction_decision(
        SimpleNamespace(auction_long_score=0.8, auction_short_score=0.3)
    )
    assert decision.direction == "long"
    assert decision.quantity == 1.0


def test_trade_row_is_converted_to_domain_event():
    event = _row_to_event(
        {
            "event_type": "trade",
            "symbol": "BTCUSDT",
            "trade_id": 1,
            "price": 100.0,
            "quantity": 0.5,
            "side": "buy",
            "is_buyer_maker": False,
            "timestamp": "2026-01-01T00:00:00Z",
        }
    )
    assert isinstance(event, TradeTick)
    assert event.price == 100.0


def test_orderbook_row_is_converted_to_domain_event():
    event = _row_to_event(
        {
            "event_type": "orderbook",
            "symbol": "BTCUSDT",
            "last_update_id": 10,
            "bid_levels": [{"price": 99.0, "qty": 2.0}],
            "ask_levels": [{"price": 101.0, "qty": 1.0}],
            "timestamp": "2026-01-01T00:00:00Z",
        }
    )
    assert isinstance(event, OrderBookSnapshot)
    assert event.best_bid == 99.0
    assert event.best_ask == 101.0
