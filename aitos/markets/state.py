"""Cross-market regime and state aggregation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timezone


class MarketRegime(str, Enum):
    UNKNOWN = "unknown"
    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"
    TRANSITION = "transition"
    LOW_VOLATILITY = "low_volatility"
    HIGH_VOLATILITY = "high_volatility"


@dataclass(frozen=True, slots=True)
class GlobalMarketState:
    """Immutable snapshot consumed by strategy/risk layers."""

    regime: MarketRegime = MarketRegime.UNKNOWN
    risk_score: float = 0.0
    volatility_score: float = 0.0
    liquidity_score: float = 0.0
    usd_score: float = 0.0
    rates_score: float = 0.0
    equity_score: float = 0.0
    crypto_score: float = 0.0
    confidence: float = 0.0
    features: dict[str, float] = field(default_factory=dict)
    as_of: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        for name in ("risk_score", "volatility_score", "liquidity_score", "usd_score", "rates_score", "equity_score", "crypto_score", "confidence"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")


class MarketStateBuilder:
    """Deterministic baseline state classifier; ML can replace this later."""

    @staticmethod
    def build(*, volatility: float, risk: float, liquidity: float, confidence: float = 0.5, features: dict[str, float] | None = None) -> GlobalMarketState:
        volatility = min(1.0, max(0.0, volatility))
        risk = min(1.0, max(0.0, risk))
        liquidity = min(1.0, max(0.0, liquidity))
        confidence = min(1.0, max(0.0, confidence))
        if volatility >= 0.75:
            regime = MarketRegime.HIGH_VOLATILITY
        elif volatility <= 0.25:
            regime = MarketRegime.LOW_VOLATILITY
        elif risk >= 0.65:
            regime = MarketRegime.RISK_ON
        elif risk <= 0.35:
            regime = MarketRegime.RISK_OFF
        else:
            regime = MarketRegime.TRANSITION
        return GlobalMarketState(
            regime=regime,
            risk_score=risk,
            volatility_score=volatility,
            liquidity_score=liquidity,
            confidence=confidence,
            features=features or {},
        )
