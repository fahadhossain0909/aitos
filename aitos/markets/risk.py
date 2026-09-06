"""Market-agnostic pre-trade risk gate."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import Instrument
from .portfolio import Portfolio
from .state import GlobalMarketState


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    max_notional: float
    reason: str
    risk_score: float


class RiskEngine:
    """Apply universal risk limits before an execution intent reaches a venue."""

    def __init__(
        self,
        *,
        max_gross_leverage: float = 2.0,
        max_single_position_fraction: float = 0.35,
    ) -> None:
        if max_gross_leverage <= 0 or not 0 < max_single_position_fraction <= 1:
            raise ValueError("invalid risk limits")
        self.max_gross_leverage = max_gross_leverage
        self.max_single_position_fraction = max_single_position_fraction

    def evaluate(
        self,
        *,
        instrument: Instrument,
        requested_notional: float,
        portfolio: Portfolio,
        state: GlobalMarketState,
        equity: float,
    ) -> RiskDecision:
        if equity <= 0:
            return RiskDecision(False, 0.0, "non_positive_equity", 1.0)
        if requested_notional <= 0:
            return RiskDecision(False, 0.0, "non_positive_notional", 1.0)
        gross_limit = equity * self.max_gross_leverage
        concentration_limit = equity * self.max_single_position_fraction
        remaining_gross = max(0.0, gross_limit - portfolio.gross_notional)
        max_notional = min(remaining_gross, concentration_limit)
        risk_score = min(
            1.0, state.volatility_score * 0.6 + (1.0 - state.liquidity_score) * 0.4
        )
        # Volatility and the aggregate risk score are independent controls:
        # high volatility halves deployable size, then elevated composite risk
        # applies a second floor-capped reduction.
        if state.volatility_score >= 0.75:
            max_notional *= 0.5
        if risk_score >= 0.5:
            max_notional *= max(0.5, 1.0 - risk_score)
        allowed = requested_notional <= max_notional
        reason = "approved" if allowed else "risk_limit_exceeded"
        return RiskDecision(allowed, max_notional, reason, risk_score)
