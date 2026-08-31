"""Market State Engine — Phase A of the Market-Path / Exit-Intelligence architecture.

Canonical, deterministic representation of the current market condition.
Consumes (does not modify) existing order-flow, liquidity, indicators and
live-state modules.
"""

from aitos.intelligence.market_state.engine import MarketStateEngine
from aitos.intelligence.market_state.models import (
    AuctionState,
    LiquidityBias,
    MarketState,
    MomentumState,
    OrderFlowBias,
    Regime,
    StructureBias,
    VolatilityRegime,
)

__all__ = [
    "MarketStateEngine",
    "MarketState",
    "Regime",
    "VolatilityRegime",
    "AuctionState",
    "OrderFlowBias",
    "LiquidityBias",
    "MomentumState",
    "StructureBias",
]
