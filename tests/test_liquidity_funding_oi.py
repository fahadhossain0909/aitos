from datetime import datetime, timezone

from aitos.intelligence.funding import funding_rate_score
from aitos.intelligence.liquidity import liquidity_quality_score
from aitos.intelligence.open_interest import oi_trend_score
from aitos.models.market import FundingRate, OpenInterest, OrderBookSnapshot
from aitos.models.trade import TradeSide

NOW = datetime.now(timezone.utc)


def make_book(bid=100.0, ask=100.05, bid_qty=10.0, ask_qty=10.0):
    return OrderBookSnapshot(
        symbol="TEST",
        bids=((bid, bid_qty),),
        asks=((ask, ask_qty),),
        last_update_id=1,
        timestamp=NOW,
    )


def test_liquidity_score_tight_balanced_book_scores_high():
    book = make_book(bid=100.0, ask=100.01, bid_qty=10.0, ask_qty=10.0)
    score = liquidity_quality_score(book, typical_spread_bps=5.0)
    assert score > 7.0


def test_liquidity_score_wide_spread_scores_lower_than_tight_spread():
    tight = make_book(bid=100.0, ask=100.01, bid_qty=10.0, ask_qty=10.0)
    wide = make_book(bid=100.0, ask=101.0, bid_qty=10.0, ask_qty=10.0)  # ~100bps spread
    assert liquidity_quality_score(
        wide, typical_spread_bps=5.0
    ) < liquidity_quality_score(tight, typical_spread_bps=5.0)


def test_liquidity_score_imbalanced_depth_penalized():
    balanced = make_book(bid_qty=10.0, ask_qty=10.0)
    imbalanced = make_book(bid_qty=100.0, ask_qty=1.0)
    assert liquidity_quality_score(balanced) > liquidity_quality_score(imbalanced)


def test_liquidity_score_empty_book_is_zero():
    empty = OrderBookSnapshot(
        symbol="TEST", bids=(), asks=(), last_update_id=1, timestamp=NOW
    )
    assert liquidity_quality_score(empty) == 0.0


def test_funding_rate_score_favors_longs_when_rate_negative():
    funding = FundingRate(
        symbol="BTCUSDT", funding_rate=-0.0005, funding_time=NOW, mark_price=65000.0
    )
    long_score = funding_rate_score(funding, TradeSide.LONG)
    short_score = funding_rate_score(funding, TradeSide.SHORT)
    assert long_score > 5.0
    assert short_score < 5.0


def test_funding_rate_score_favors_shorts_when_rate_positive():
    funding = FundingRate(
        symbol="BTCUSDT", funding_rate=0.0005, funding_time=NOW, mark_price=65000.0
    )
    assert funding_rate_score(funding, TradeSide.SHORT) > 5.0
    assert funding_rate_score(funding, TradeSide.LONG) < 5.0


def test_funding_rate_score_neutral_for_zero_rate():
    funding = FundingRate(
        symbol="BTCUSDT", funding_rate=0.0, funding_time=NOW, mark_price=65000.0
    )
    assert funding_rate_score(funding, TradeSide.LONG) == 5.0


def test_oi_trend_score_neutral_when_no_previous_reading():
    current = OpenInterest(symbol="BTCUSDT", open_interest=1000.0, timestamp=NOW)
    assert oi_trend_score(current, None, TradeSide.LONG, price_moved_up=True) == 5.0


def test_oi_trend_score_high_when_rising_oi_confirms_long():
    previous = OpenInterest(symbol="BTCUSDT", open_interest=1000.0, timestamp=NOW)
    current = OpenInterest(
        symbol="BTCUSDT", open_interest=1150.0, timestamp=NOW
    )  # +15%
    score = oi_trend_score(current, previous, TradeSide.LONG, price_moved_up=True)
    assert score > 5.0


def test_oi_trend_score_low_when_rising_oi_contradicts_long():
    previous = OpenInterest(symbol="BTCUSDT", open_interest=1000.0, timestamp=NOW)
    current = OpenInterest(symbol="BTCUSDT", open_interest=1150.0, timestamp=NOW)
    score = oi_trend_score(current, previous, TradeSide.LONG, price_moved_up=False)
    assert score < 5.0


def test_oi_trend_score_flat_change_is_neutral():
    previous = OpenInterest(symbol="BTCUSDT", open_interest=1000.0, timestamp=NOW)
    current = OpenInterest(
        symbol="BTCUSDT", open_interest=1002.0, timestamp=NOW
    )  # 0.2% change
    assert oi_trend_score(current, previous, TradeSide.LONG, price_moved_up=True) == 5.0
