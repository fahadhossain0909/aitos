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
