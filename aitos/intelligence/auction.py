"""Auction Market Theory-inspired context from OHLCV.

This is an honest OHLCV approximation: without intrabar volume-at-price we
cannot claim a true footprint/volume-profile value area. It measures balance,
range location, acceptance and breakout extension instead.
"""

from __future__ import annotations

from typing import Sequence, Tuple

from aitos.models.market import Kline


def auction_context_score(
    klines: Sequence[Kline], direction: str, lookback: int = 20
) -> float:
    if len(klines) < 5 or direction not in {"long", "short"}:
        return 5.0
    window = list(klines[-lookback:])
    high = max(k.high for k in window)
    low = min(k.low for k in window)
    width = high - low
    if width <= 0:
        return 5.0
    last = window[-1].close
    location = (last - low) / width
    recent_ranges = [k.high - k.low for k in window]
    avg_range = sum(recent_ranges) / len(recent_ranges) if recent_ranges else 0.0
    extension = (last - window[-2].close) / avg_range if avg_range else 0.0
    # Longs prefer upper acceptance without an extreme blow-off; shorts prefer
    # lower acceptance without an extreme downside extension.
    if direction == "long":
        score = 5.0 + location * 4.0 - max(0.0, extension - 1.5) * 2.0
    else:
        score = 9.0 - location * 4.0 - max(0.0, -extension - 1.5) * 2.0
    return round(max(0.0, min(10.0, score)), 2)


def balance_score(klines: Sequence[Kline], lookback: int = 20) -> float:
    if len(klines) < 5:
        return 0.0
    window = list(klines[-lookback:])
    ranges = [k.high - k.low for k in window]
    closes = [k.close for k in window]
    if not ranges or not closes:
        return 0.0
    price_range = max(closes) - min(closes)
    avg_range = sum(ranges) / len(ranges)
    if avg_range <= 0:
        return 10.0
    return round(
        max(0.0, min(10.0, 10.0 - (price_range / (avg_range * len(window))) * 10.0)), 2
    )
