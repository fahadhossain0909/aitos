"""Funding rate scoring — spec section 32.1's "Funding rate (cost of
carry)" dimension. Positive funding means longs pay shorts (bearish tilt
in perp positioning); negative means shorts pay longs. A trade aligned
with the direction that *gets paid* scores higher.
"""

from __future__ import annotations

from aitos.models.market import FundingRate
from aitos.models.trade import TradeSide

# Typical funding rates cluster around ±0.01%/8h; anything beyond ~0.05% is
# considered a strong signal for this scoring purpose.
STRONG_FUNDING_RATE = 0.0005


def funding_rate_score(funding: FundingRate, side: TradeSide) -> float:
    """0-10: 10 means the proposed side is being paid a strong funding rate,
    5 means funding is roughly neutral, 0 means the side is paying a strong
    rate against itself."""
    rate = funding.funding_rate
    # Longs are paid when funding is negative; shorts are paid when funding is positive.
    signed_for_side = -rate if side == TradeSide.LONG else rate
    normalized = (
        signed_for_side / STRONG_FUNDING_RATE
    )  # roughly -1..1 for typical rates
    return round(max(0.0, min(10.0, 5.0 + normalized * 5.0)), 2)
