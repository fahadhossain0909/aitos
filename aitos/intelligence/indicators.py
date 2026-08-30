"""Technical indicators computed from OHLCV history — spec section 29.1's
Market Structure / Market Regime / CVD rows, implemented as pure functions
over ``List[Kline]`` so they're trivially unit-testable with synthetic data
and reusable by both the Opportunity Scanner and (later) live agents.

All functions expect klines in chronological order (oldest first).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from aitos.models.market import Kline


def true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def average_true_range(klines: Sequence[Kline], period: int = 14) -> float:
    """Wilder's ATR. Returns 0.0 if there isn't enough history."""
    if len(klines) < 2:
        return 0.0
    trs = [
        true_range(klines[i].high, klines[i].low, klines[i - 1].close)
        for i in range(1, len(klines))
    ]
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window) if window else 0.0


def atr_percentile(
    klines: Sequence[Kline], period: int = 14, lookback: int = 100
) -> float:
    """Where the current ATR sits (0-100) relative to its own recent history —
    a simple, self-normalizing volatility-regime proxy that needs no
    external calibration."""
    if len(klines) < period + 2:
        return 50.0
    atrs: list[float] = []
    start = max(1, len(klines) - lookback)
    for end in range(start + period, len(klines) + 1):
        atrs.append(average_true_range(klines[max(0, end - period - 1) : end], period))
    if not atrs:
        return 50.0
    current = atrs[-1]
    less = sum(1 for a in atrs if a < current)
    equal = sum(1 for a in atrs if a == current)
    rank = less + 0.5 * equal
    return round(rank / len(atrs) * 100, 2)


def _directional_movement(
    klines: Sequence[Kline],
) -> tuple[list[float], list[float], list[float]]:
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(klines)):
        up_move = klines[i].high - klines[i - 1].high
        down_move = klines[i - 1].low - klines[i].low
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        trs.append(true_range(klines[i].high, klines[i].low, klines[i - 1].close))
    return plus_dm, minus_dm, trs


def adx(klines: Sequence[Kline], period: int = 14) -> float:
    """Average Directional Index (0-100) — trend strength regardless of direction."""
    if len(klines) < period + 2:
        return 0.0
    plus_dm, minus_dm, trs = _directional_movement(klines)

    def _smooth(values: list[float]) -> list[float]:
        smoothed = [sum(values[:period])]
        for v in values[period:]:
            smoothed.append(smoothed[-1] - (smoothed[-1] / period) + v)
        return smoothed

    smoothed_plus = _smooth(plus_dm)
    smoothed_minus = _smooth(minus_dm)
    smoothed_tr = _smooth(trs)

    dx_values = []
    for i in range(len(smoothed_tr)):
        if smoothed_tr[i] == 0:
            continue
        plus_di = 100 * smoothed_plus[i] / smoothed_tr[i]
        minus_di = 100 * smoothed_minus[i] / smoothed_tr[i]
        di_sum = plus_di + minus_di
        dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0.0
        dx_values.append(dx)

    if not dx_values:
        return 0.0
    window = dx_values[-period:] if len(dx_values) >= period else dx_values
    return round(sum(window) / len(window), 2)


def cumulative_volume_delta(klines: Sequence[Kline]) -> float:
    """Sum of (taker-buy-volume - taker-sell-volume) across the window."""
    total = 0.0
    for k in klines:
        taker_sell_volume = k.volume - k.taker_buy_volume
        total += k.taker_buy_volume - taker_sell_volume
    return total


def cvd_trend_score(klines: Sequence[Kline], lookback: int = 20) -> float:
    """0-10 score: how strongly recent order flow leans buy (10) vs sell (0)."""
    window = klines[-lookback:] if len(klines) >= lookback else klines
    if not window:
        return 5.0
    delta = cumulative_volume_delta(window)
    total_volume = sum(k.volume for k in window)
    if total_volume == 0:
        return 5.0
    normalized = delta / total_volume
    return round(max(0.0, min(10.0, 5.0 + normalized * 5.0)), 2)


def detect_structure_break(
    klines: Sequence[Kline], swing_lookback: int = 10
) -> tuple[str, float]:
    """Simplified Break-of-Structure (BOS) detector. Returns (direction, strength 0-10)."""
    if len(klines) < swing_lookback + 1:
        return "none", 0.0

    swing_window = klines[-(swing_lookback + 1) : -1]
    swing_high = max(k.high for k in swing_window)
    swing_low = min(k.low for k in swing_window)
    latest_close = klines[-1].close
    swing_range = swing_high - swing_low
    if swing_range <= 0:
        return "none", 0.0

    if latest_close > swing_high:
        strength = min(10.0, (latest_close - swing_high) / swing_range * 10)
        return "bullish_bos", round(strength, 2)
    if latest_close < swing_low:
        strength = min(10.0, (swing_low - latest_close) / swing_range * 10)
        return "bearish_bos", round(strength, 2)
    return "none", 0.0


def classify_regime(klines: Sequence[Kline], adx_period: int = 14) -> str:
    """trending | ranging | volatile | unknown"""
    if len(klines) < adx_period + 2:
        return "unknown"
    trend_strength = adx(klines, adx_period)
    vol_percentile = atr_percentile(klines, adx_period)
    if vol_percentile >= 85:
        return "volatile"
    if trend_strength >= 25:
        return "trending"
    return "ranging"


def pearson_correlation(series_a: Sequence[float], series_b: Sequence[float]) -> float:
    """Standard Pearson correlation coefficient, -1..1."""
    n = min(len(series_a), len(series_b))
    if n < 2:
        return 0.0
    a, b = list(series_a[-n:]), list(series_b[-n:])
    mean_a, mean_b = sum(a) / n, sum(b) / n
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((x - mean_b) ** 2 for x in b)
    denom = math.sqrt(var_a * var_b)
    return round(cov / denom, 4) if denom > 0 else 0.0


def returns(klines: Sequence[Kline]) -> list[float]:
    return [
        (klines[i].close - klines[i - 1].close) / klines[i - 1].close
        for i in range(1, len(klines))
        if klines[i - 1].close != 0
    ]


def lead_lag_score(
    symbol_klines: Sequence[Kline], reference_klines: Sequence[Kline], lag: int = 1
) -> float:
    """0-10 score: how well reference past returns predict symbol current returns."""
    symbol_returns = returns(symbol_klines)
    reference_returns = returns(reference_klines)
    if lag < 1 or len(symbol_returns) <= lag or len(reference_returns) <= lag:
        return 5.0
    lagged_reference = reference_returns[:-lag] if lag > 0 else reference_returns
    aligned_symbol = symbol_returns[lag:]
    corr = pearson_correlation(lagged_reference, aligned_symbol)
    return round(max(0.0, min(10.0, 5.0 + corr * 5.0)), 2)
