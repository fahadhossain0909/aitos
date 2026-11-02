"""Entry-timing diagnostics based on completed price structure."""

from __future__ import annotations

from collections.abc import Sequence

from aitos.models.market import Kline


def breakout_acceptance_score(
    klines: Sequence[Kline], direction: str, *, atr: float, lookback: int = 8
) -> float:
    """Score whether a structural break is holding beyond its prior boundary.

    A close beyond the boundary is stronger than an intrabar wick. A second
    close or a successful boundary retest earns the strongest score. This is
    a ranking input, not a standalone entry trigger.
    """
    side = direction.lower()
    if len(klines) < max(lookback + 2, 4) or atr <= 0 or side not in {"long", "short"}:
        return 5.0
    prior = klines[-(lookback + 2):-2]
    if not prior:
        return 5.0
    previous, current = klines[-2], klines[-1]
    if side == "long":
        boundary = max(float(c.high) for c in prior)
        beyond = float(current.close) > boundary
        held = float(previous.close) > boundary
        retested = float(current.low) <= boundary + 0.25 * atr and beyond
    else:
        boundary = min(float(c.low) for c in prior)
        beyond = float(current.close) < boundary
        held = float(previous.close) < boundary
        retested = float(current.high) >= boundary - 0.25 * atr and beyond
    if held:
        return 9.0
    if retested:
        return 8.0
    if beyond:
        return 6.0
    return 2.0


def relative_strength_score(
    symbol_klines: Sequence[Kline],
    reference_klines: Sequence[Kline],
    direction: str,
    *,
    lookback: int = 20,
) -> float:
    """Direction-aware relative return versus the BTC reference (0-10)."""
    count = min(len(symbol_klines), len(reference_klines), lookback)
    if count < 3:
        return 5.0
    symbol_start = float(symbol_klines[-count].close)
    symbol_end = float(symbol_klines[-1].close)
    reference_start = float(reference_klines[-count].close)
    reference_end = float(reference_klines[-1].close)
    if min(symbol_start, symbol_end, reference_start, reference_end) <= 0:
        return 5.0
    excess = (symbol_end / symbol_start - 1.0) - (reference_end / reference_start - 1.0)
    signed_excess = excess if direction.lower() == "long" else -excess
    return round(max(0.0, min(10.0, 5.0 + signed_excess * 100.0)), 2)
