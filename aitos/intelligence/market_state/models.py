"""Canonical market-state models for the Exit-Intelligence architecture.

All enums and the MarketState dataclass are frozen / immutable so that
downstream modules (Path Planner, Exit Engine) can safely cache and
compare snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Regime(str, Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGE = "RANGE"
    TRANSITION = "TRANSITION"


class VolatilityRegime(str, Enum):
    CONTRACTING = "CONTRACTING"
    NORMAL = "NORMAL"
    EXPANDING = "EXPANDING"


class AuctionState(str, Enum):
    ACCEPTANCE_ABOVE_VALUE = "ACCEPTANCE_ABOVE_VALUE"
    ACCEPTANCE_BELOW_VALUE = "ACCEPTANCE_BELOW_VALUE"
    ACCEPTANCE_INSIDE_VALUE = "ACCEPTANCE_INSIDE_VALUE"
    REJECTION_OF_HIGHS = "REJECTION_OF_HIGHS"
    REJECTION_OF_LOWS = "REJECTION_OF_LOWS"
    BALANCED = "BALANCED"
    UNKNOWN = "UNKNOWN"


class OrderFlowBias(str, Enum):
    BUYER_DOMINANT = "BUYER_DOMINANT"
    SELLER_DOMINANT = "SELLER_DOMINANT"
    NEUTRAL = "NEUTRAL"


class LiquidityBias(str, Enum):
    UPSIDE_LIQUIDITY_HIGH = "UPSIDE_LIQUIDITY_HIGH"
    DOWNSIDE_LIQUIDITY_HIGH = "DOWNSIDE_LIQUIDITY_HIGH"
    BALANCED = "BALANCED"
    THIN = "THIN"
    UNKNOWN = "UNKNOWN"


class MomentumState(str, Enum):
    STRONG = "STRONG"
    MODERATING = "MODERATING"
    WEAK = "WEAK"
    EXHAUSTED = "EXHAUSTED"


class StructureBias(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    RANGE = "RANGE"
    BROKEN = "BROKEN"


@dataclass(frozen=True)
class MarketState:
    """Single source of truth for the current market condition of a symbol.

    Downstream modules must consume this object rather than re-deriving
    regime / bias from raw indicators themselves. This prevents divergent
    decisions across Path Planner, Exit Engine and Entry Engine.
    """

    symbol: str
    timestamp: datetime
    mid_price: float

    regime: Regime
    trend_strength: float  # 0.0 – 1.0
    volatility_regime: VolatilityRegime
    auction_state: AuctionState
    order_flow_bias: OrderFlowBias
    liquidity_bias: LiquidityBias
    momentum: MomentumState
    structure: StructureBias
    reversal_risk: float  # 0.0 – 1.0

    # Fully explainable feature bag (name → value). Every scoring decision
    # downstream can point back to these numbers.
    features: dict[str, float] = field(default_factory=dict)

    # Optional free-form notes for XAI / journal
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
            "mid_price": self.mid_price,
            "regime": self.regime.value,
            "trend_strength": self.trend_strength,
            "volatility_regime": self.volatility_regime.value,
            "auction_state": self.auction_state.value,
            "order_flow_bias": self.order_flow_bias.value,
            "liquidity_bias": self.liquidity_bias.value,
            "momentum": self.momentum.value,
            "structure": self.structure.value,
            "reversal_risk": self.reversal_risk,
            "features": dict(self.features),
            "notes": list(self.notes),
        }
