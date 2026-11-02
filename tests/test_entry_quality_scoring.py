from datetime import datetime, timedelta, timezone

from aitos.intelligence.entry_quality import (
    breakout_acceptance_score,
    relative_strength_score,
)
from aitos.models.market import Kline


def _klines(closes, *, high_offset=0.5, low_offset=0.5):
    now = datetime.now(timezone.utc)
    rows = []
    for i, close in enumerate(closes):
        rows.append(
            Kline(
                symbol="BTCUSDT",
                timeframe="1m",
                open_time=now + timedelta(minutes=i),
                close_time=now + timedelta(minutes=i + 1),
                open=close,
                high=close + high_offset,
                low=close - low_offset,
                close=close,
                volume=100.0,
                quote_volume=10000.0,
                trades_count=100,
                taker_buy_volume=50.0,
                taker_buy_quote_volume=5000.0,
                is_closed=True,
            )
        )
    return rows


def test_relative_strength_is_direction_aware():
    reference = _klines([100 + i for i in range(20)])
    stronger = _klines([100 + i * 1.2 for i in range(20)])
    assert relative_strength_score(stronger, reference, "LONG") > 5.0
    assert relative_strength_score(stronger, reference, "SHORT") < 5.0


def test_breakout_acceptance_rewards_two_closes_beyond_boundary():
    rows = _klines(
        [100, 100, 100, 100, 100, 100, 102, 103], high_offset=0.2, low_offset=0.2
    )
    assert breakout_acceptance_score(rows, "LONG", atr=1.0) >= 8.0


def test_breakout_acceptance_penalizes_close_back_inside_range():
    rows = _klines(
        [100, 100, 100, 100, 100, 100, 102, 99.5], high_offset=0.2, low_offset=0.2
    )
    assert breakout_acceptance_score(rows, "LONG", atr=1.0) == 2.0
