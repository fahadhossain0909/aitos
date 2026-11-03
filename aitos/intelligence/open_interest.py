"""Open interest trend scoring — spec section 32.1's "Open Interest trend
(conviction)" dimension. Rising OI alongside a directional move suggests
new money confirming the trend (higher conviction); rising OI against the
move suggests building opposition.
"""

from __future__ import annotations

from aitos.models.market import OpenInterest
from aitos.models.trade import TradeSide

# Below this relative change, treat OI as flat (avoid noise-driven scores).
FLAT_THRESHOLD_PCT = 1.0
STRONG_CHANGE_PCT = 10.0


def oi_trend_score(
    current: OpenInterest,
    previous: OpenInterest | None,
    side: TradeSide,
    price_moved_up: bool,
) -> float:
    """0-10: rising OI that agrees with the recent price direction (and
    with the proposed trade side) scores highest; rising OI that
    contradicts it scores lowest. Flat or unknown OI is neutral (5.0)."""
    if previous is None or previous.open_interest <= 0:
        return 5.0

    pct_change = (
        (current.open_interest - previous.open_interest) / previous.open_interest * 100
    )
    if abs(pct_change) < FLAT_THRESHOLD_PCT:
        return 5.0

    price_confirms_side = price_moved_up == (side == TradeSide.LONG)
    magnitude = min(abs(pct_change) / STRONG_CHANGE_PCT, 1.0)

    if pct_change > 0:
        # New money entering. Good if it agrees with our side's direction.
        signed = magnitude if price_confirms_side else -magnitude
    else:
        # OI unwinding (position closing) — mildly against conviction either way.
        signed = -magnitude * 0.5

    return round(max(0.0, min(10.0, 5.0 + signed * 5.0)), 2)
