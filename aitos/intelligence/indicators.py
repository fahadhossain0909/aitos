"""Technical indicators computed from OHLCV history — spec section 29.1's
Market Structure / Market Regime / CVD rows, implemented as pure functions
over ``List[Kline]`` so they're trivially unit-testable with synthetic data
and reusable by both the Opportunity Scanner and (later) live agents.

All functions expect klines in chronological order (oldest first).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

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
    rank = less + 0.5 * equal  # standard tie-aware percentile rank
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


def di_plus_minus(klines: Sequence[Kline], period: int = 14) -> tuple[float, float]:
    """Return (+DI, -DI) on the same 0-100 scale as ADX uses."""
    if len(klines) < period + 2:
        return 0.0, 0.0
    plus_dm, minus_dm, trs = _directional_movement(klines)

    def _smooth(values: list[float]) -> list[float]:
        smoothed = [sum(values[:period])]
        for v in values[period:]:
            smoothed.append(smoothed[-1] - (smoothed[-1] / period) + v)
        return smoothed

    smoothed_plus = _smooth(plus_dm)
    smoothed_minus = _smooth(minus_dm)
    smoothed_tr = _smooth(trs)
    if not smoothed_tr or smoothed_tr[-1] == 0:
        return 0.0, 0.0
    plus_di = 100 * smoothed_plus[-1] / smoothed_tr[-1]
    minus_di = 100 * smoothed_minus[-1] / smoothed_tr[-1]
    return round(plus_di, 2), round(minus_di, 2)


def cumulative_volume_delta(klines: Sequence[Kline]) -> float:
    """Sum of (taker-buy-volume - taker-sell-volume) across the window — a
    per-candle proxy for order-flow bias since we don't have raw footprint
    data at this layer. Positive = net buying pressure."""
    total = 0.0
    for k in klines:
        taker_sell_volume = k.volume - k.taker_buy_volume
        total += k.taker_buy_volume - taker_sell_volume
    return total


def cvd_trend_score(klines: Sequence[Kline], lookback: int = 20) -> float:
    """0-10 score: how strongly recent order flow leans buy (10) vs sell (0),
    normalized by total volume in the window so it's comparable across symbols."""
    window = klines[-lookback:] if len(klines) >= lookback else klines
    if not window:
        return 5.0
    delta = cumulative_volume_delta(window)
    total_volume = sum(k.volume for k in window)
    if total_volume == 0:
        return 5.0
    normalized = delta / total_volume  # roughly in [-1, 1]
    return round(max(0.0, min(10.0, 5.0 + normalized * 5.0)), 2)


def detect_structure_break(
    klines: Sequence[Kline], swing_lookback: int = 10
) -> tuple[str, float]:
    """Very simplified Break-of-Structure (BOS) detector: compares the most
    recent close against the highest high / lowest low of the preceding
    swing window. Returns (direction, strength 0-10).

    Prefer ``analyze_market_structure`` for HH/HL/LH/LL + CHoCH. This
    function is retained for backward compatibility with existing callers
    and tests.
    """
    structure = analyze_market_structure(klines, swing_lookback=swing_lookback)
    return structure.bos_direction, structure.bos_strength


@dataclass(frozen=True)
class MarketStructure:
    """Structured market-structure snapshot.

    ``bias`` is one of: bullish, bearish, neutral.
    ``bos_direction`` mirrors the legacy detector: bullish_bos | bearish_bos | none.
    ``event`` is bos | choch | none.
    """

    bias: str
    event: str
    bos_direction: str
    bos_strength: float
    swing_high: float
    swing_low: float
    last_swing_type: str  # HH | HL | LH | LL | none
    structure_score: float  # 0-10 directional strength for scoring


def _local_swing_highs_lows(
    klines: Sequence[Kline], left: int = 2, right: int = 2
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Return confirmed swing highs and lows as (index, price) pairs."""
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    n = len(klines)
    if n < left + right + 1:
        return highs, lows
    for i in range(left, n - right):
        window_highs = [klines[j].high for j in range(i - left, i + right + 1)]
        window_lows = [klines[j].low for j in range(i - left, i + right + 1)]
        if klines[i].high == max(window_highs):
            highs.append((i, klines[i].high))
        if klines[i].low == min(window_lows):
            lows.append((i, klines[i].low))
    return highs, lows


def analyze_market_structure(
    klines: Sequence[Kline], swing_lookback: int = 10, pivot: int = 2
) -> MarketStructure:
    """Track swing highs/lows, classify HH/HL/LH/LL, and detect BOS / CHoCH.

    Algorithm (conservative):
    1. Find confirmed local pivots.
    2. Compare the two most recent swing highs and lows for structure labels.
    3. BOS: close breaks the most recent opposite swing in the trend direction.
    4. CHoCH: close breaks the structure against the prevailing bias.
    """
    empty = MarketStructure(
        bias="neutral",
        event="none",
        bos_direction="none",
        bos_strength=0.0,
        swing_high=0.0,
        swing_low=0.0,
        last_swing_type="none",
        structure_score=5.0,
    )
    if len(klines) < max(swing_lookback + 1, pivot * 2 + 3):
        return empty

    highs, lows = _local_swing_highs_lows(klines, left=pivot, right=pivot)
    # Fallback to window extremes when pivots are sparse (short history).
    swing_window = klines[-(swing_lookback + 1) : -1]
    window_high = max(k.high for k in swing_window)
    window_low = min(k.low for k in swing_window)
    latest_close = klines[-1].close
    swing_range = window_high - window_low
    if swing_range <= 0:
        return empty

    last_swing_type = "none"
    bias = "neutral"
    if len(highs) >= 2 and len(lows) >= 2:
        h1, h2 = highs[-2][1], highs[-1][1]
        l1, l2 = lows[-2][1], lows[-1][1]
        higher_high = h2 > h1
        higher_low = l2 > l1
        lower_high = h2 < h1
        lower_low = l2 < l1
        if higher_high and higher_low:
            bias, last_swing_type = "bullish", "HH"
        elif lower_high and lower_low:
            bias, last_swing_type = "bearish", "LL"
        elif higher_high:
            last_swing_type = "HH"
        elif lower_high:
            last_swing_type = "LH"
        elif higher_low:
            last_swing_type = "HL"
        elif lower_low:
            last_swing_type = "LL"

    event = "none"
    bos_direction = "none"
    bos_strength = 0.0

    if latest_close > window_high:
        bos_strength = min(10.0, (latest_close - window_high) / swing_range * 10)
        bos_direction = "bullish_bos"
        # CHoCH if prior bias was bearish; otherwise continuation BOS.
        event = "choch" if bias == "bearish" else "bos"
        bias = "bullish"
    elif latest_close < window_low:
        bos_strength = min(10.0, (window_low - latest_close) / swing_range * 10)
        bos_direction = "bearish_bos"
        event = "choch" if bias == "bullish" else "bos"
        bias = "bearish"

    if bias == "bullish":
        structure_score = round(min(10.0, 5.0 + bos_strength * 0.5 + 1.0), 2)
    elif bias == "bearish":
        structure_score = round(max(0.0, 5.0 - bos_strength * 0.5 - 1.0), 2)
    else:
        structure_score = 5.0

    return MarketStructure(
        bias=bias,
        event=event,
        bos_direction=bos_direction,
        bos_strength=round(bos_strength, 2),
        swing_high=window_high,
        swing_low=window_low,
        last_swing_type=last_swing_type,
        structure_score=structure_score,
    )


def classify_regime(klines: Sequence[Kline], adx_period: int = 14) -> str:
    """Primary regime label used by the scanner / RL context.

    Returns one of:
      trending | ranging | volatile | compression | expansion | unknown

    Compression = low ADX + low ATR percentile (coiling).
    Expansion = rising volatility after a quiet stretch (ATR percentile mid-high
    with accelerating true range) — approximated via high ATR percentile
    without extreme ADX.
    """
    if len(klines) < adx_period + 2:
        return "unknown"
    trend_strength = adx(klines, adx_period)
    vol_percentile = atr_percentile(klines, adx_period)
    if vol_percentile >= 85:
        return "volatile"
    if trend_strength >= 25:
        return "trending"
    if vol_percentile <= 25 and trend_strength < 20:
        return "compression"
    if vol_percentile >= 65 and trend_strength < 25:
        return "expansion"
    return "ranging"


def rsi(klines: Sequence[Kline], period: int = 14) -> float:
    """Wilder RSI (0-100). Neutral 50 when history is insufficient."""
    if len(klines) < period + 1:
        return 50.0
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(klines)):
        change = klines[i].close - klines[i - 1].close
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def roc(klines: Sequence[Kline], period: int = 10) -> float:
    """Rate of change as a percentage over ``period`` bars."""
    if len(klines) <= period or klines[-(period + 1)].close == 0:
        return 0.0
    prev = klines[-(period + 1)].close
    return round((klines[-1].close - prev) / prev * 100.0, 4)


def ema(values: Sequence[float], period: int) -> list[float]:
    """Exponential moving average series for a numeric sequence."""
    if not values or period < 1:
        return []
    alpha = 2.0 / (period + 1)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(alpha * float(v) + (1.0 - alpha) * out[-1])
    return out


def ema_slope_score(klines: Sequence[Kline], period: int = 20) -> float:
    """0-10 score from recent EMA slope (up = high, down = low)."""
    if len(klines) < period + 3:
        return 5.0
    closes = [k.close for k in klines]
    series = ema(closes, period)
    if len(series) < 3 or series[-3] == 0:
        return 5.0
    slope = (series[-1] - series[-3]) / abs(series[-3])
    # ±2% over 2 bars maps toward extremes
    return round(max(0.0, min(10.0, 5.0 + slope * 250.0)), 2)


def momentum_score(klines: Sequence[Kline]) -> float:
    """Orthogonal momentum blend: RSI + ROC + EMA slope → 0-10.

    Intentionally light — not a classic-indicator dump. Used as an optional
    reinforcement signal, not a primary edge.
    """
    if len(klines) < 20:
        return 5.0
    r = rsi(klines)
    rate = roc(klines, period=10)
    slope = ema_slope_score(klines)
    rsi_component = max(0.0, min(10.0, r / 10.0))
    roc_component = max(0.0, min(10.0, 5.0 + rate * 2.0))
    blended = 0.4 * rsi_component + 0.3 * roc_component + 0.3 * slope
    return round(blended, 2)


def pearson_correlation(series_a: Sequence[float], series_b: Sequence[float]) -> float:
    """Standard Pearson correlation coefficient, -1..1. Returns 0.0 for
    degenerate input (mismatched/short/constant series)."""
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
    symbol_klines: Sequence[Kline],
    reference_klines: Sequence[Kline],
    lag: int = 1,
    max_lag: int = 3,
) -> float:
    """0-10 score: how well ``reference``'s past returns predict ``symbol``'s
    current returns (e.g. BTC leading an altcoin).

    Uses the best lag in ``1..max_lag`` by absolute correlation, then maps
    the signed correlation to 0-10. Persistence is rewarded when multiple
    lags agree on the sign.
    """
    symbol_returns = returns(symbol_klines)
    reference_returns = returns(reference_klines)
    if lag < 1 or len(symbol_returns) <= lag or len(reference_returns) <= lag:
        return 5.0

    best_corr = 0.0
    agreements = 0
    tested = 0
    for L in range(1, max(1, max_lag) + 1):
        if len(symbol_returns) <= L or len(reference_returns) <= L:
            continue
        lagged_reference = reference_returns[:-L]
        aligned_symbol = symbol_returns[L:]
        corr = pearson_correlation(lagged_reference, aligned_symbol)
        tested += 1
        if abs(corr) > abs(best_corr):
            best_corr = corr
        if corr * best_corr > 0 and abs(corr) >= 0.15:
            agreements += 1

    if tested == 0:
        return 5.0
    persistence_boost = 0.5 * (agreements / tested)
    score = (
        5.0
        + best_corr * 5.0
        + (persistence_boost if best_corr > 0 else -persistence_boost)
    )
    return round(max(0.0, min(10.0, score)), 2)


def volatility_regime_score(klines: Sequence[Kline]) -> float:
    """0-10 fitness for trading: prefers mid volatility, penalizes extremes."""
    pct = atr_percentile(klines)
    # sweet spot ~40-70
    return round(max(0.0, min(10.0, 10.0 - abs(pct - 55.0) / 6.0)), 2)
