from datetime import datetime, timezone

from aitos.models.market import (FundingRate, Kline, OpenInterest,
                                 OrderBookSnapshot, TradeSide, TradeTick)


def test_kline_to_dict_roundtrip_fields():
    now = datetime.now(timezone.utc)
    k = Kline(
        symbol="BTCUSDT",
        timeframe="1m",
        open_time=now,
        close_time=now,
        open=100.0,
        high=105.0,
        low=99.0,
        close=103.0,
        volume=10.0,
        quote_volume=1030.0,
        trades_count=42,
        taker_buy_volume=6.0,
        taker_buy_quote_volume=618.0,
    )
    d = k.to_dict()
    assert d["symbol"] == "BTCUSDT"
    assert d["close"] == 103.0
    assert d["is_closed"] is True


def test_order_book_spread_and_depth_ratio():
    now = datetime.now(timezone.utc)
    book = OrderBookSnapshot(
        symbol="ETHUSDT",
        bids=((3000.0, 2.0), (2999.5, 1.0)),
        asks=((3000.5, 1.5), (3001.0, 2.0)),
        last_update_id=123,
        timestamp=now,
    )
    assert book.best_bid == 3000.0
    assert book.best_ask == 3000.5
    assert round(book.spread, 2) == 0.5
    assert round(book.depth_ratio, 4) == round(3.0 / 3.5, 4)


def test_order_book_empty_ask_depth_is_infinite_ratio():
    now = datetime.now(timezone.utc)
    book = OrderBookSnapshot(
        symbol="X", bids=((1.0, 1.0),), asks=(), last_update_id=1, timestamp=now
    )
    assert book.depth_ratio == float("inf")


def test_trade_tick_side_and_dict():
    now = datetime.now(timezone.utc)
    t = TradeTick(
        symbol="BTCUSDT",
        trade_id=1,
        price=100.0,
        quantity=0.5,
        side=TradeSide.BUY,
        is_buyer_maker=False,
        timestamp=now,
    )
    assert t.to_dict()["side"] == "buy"


def test_funding_rate_and_open_interest_dicts():
    now = datetime.now(timezone.utc)
    fr = FundingRate(
        symbol="BTCUSDT", funding_rate=0.0001, funding_time=now, mark_price=65000.0
    )
    oi = OpenInterest(symbol="BTCUSDT", open_interest=12345.6, timestamp=now)
    assert fr.to_dict()["funding_rate"] == 0.0001
    assert oi.to_dict()["open_interest"] == 12345.6
