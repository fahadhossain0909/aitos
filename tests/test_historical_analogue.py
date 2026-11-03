from datetime import datetime, timedelta, timezone

from aitos.intelligence.historical_analogue import (
    infer_state_transition,
    search_historical_analogues,
)
from aitos.models.market import Kline


def _k(i: int, close: float) -> Kline:
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i)
    return Kline(
        symbol="BTCUSDT",
        timeframe="1m",
        open_time=ts,
        close_time=ts + timedelta(minutes=1),
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=100.0,
        quote_volume=100.0 * close,
        trades_count=10,
        taker_buy_volume=50.0,
        taker_buy_quote_volume=50.0 * close,
    )


def test_historical_analogue_is_prior_and_has_forward_outcome():
    # Two similar upward shapes followed by a measurable continuation, then a
    # current shape.  The search must never use the current window as a match.
    prices = [100, 102, 104, 106, 108, 110, 112, 114, 116, 118]
    prices += [118, 116, 114, 116, 118, 120, 122, 124, 126, 128]
    prices += [200, 202, 204, 206, 208, 210, 212, 214, 216, 218]
    klines = [_k(i, p) for i, p in enumerate(prices)]
    matches = search_historical_analogues(
        klines, window=5, search_back=30, top_k=5, forward_horizon=2
    )
    assert matches
    assert all(m.start_index + 5 < len(klines) - 1 for m in matches)
    assert matches[0].outcome is not None


def test_state_transition_is_explicit():
    result = infer_state_transition("compression", "expansion")
    assert result.transition_score == 1.0
    assert result.persistence == 0.5


def test_unknown_states_are_safe():
    result = infer_state_transition("", "")
    assert result.previous_state == "unknown"
    assert result.current_state == "unknown"
    assert result.transition_score == 0.0
