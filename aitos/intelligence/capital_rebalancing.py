"""Fee-aware, protection-first position switching decisions.

This policy is deliberately pure: it proposes a switch but never closes a
position, submits an order, changes a stop, or bypasses CapitalGateway. The
runtime must authorize the replacement and confirm the incumbent close
before opening the replacement.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


_BPS_TO_PCT = 0.01


def _finite_non_negative(value: float, name: str) -> float:
    value = float(value)
    if not isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return value


@dataclass(frozen=True, slots=True)
class RebalanceConfig:
    """Strict anti-churn and capital-protection policy defaults."""

    min_candidate_confidence: float = 0.85
    max_candidate_loss_probability: float = 0.35
    min_net_advantage_pct: float = 0.15
    min_incumbent_stagnation: float = 0.70
    min_hold_seconds: float = 60.0
    switch_cooldown_seconds: float = 300.0
    max_switches_per_hour: int = 2
    min_liquidity_score: float = 4.0
    min_liquidation_distance_pct: float = 1.0
    max_trade_risk_pct: float = 1.0
    max_portfolio_risk_pct: float = 5.0
    max_drawdown_pct: float = 10.0

    def __post_init__(self) -> None:
        for name in (
            "min_candidate_confidence",
            "max_candidate_loss_probability",
            "min_incumbent_stagnation",
        ):
            value = float(getattr(self, name))
            if not isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        for name in (
            "min_net_advantage_pct",
            "min_hold_seconds",
            "switch_cooldown_seconds",
            "min_liquidity_score",
            "min_liquidation_distance_pct",
            "max_trade_risk_pct",
            "max_portfolio_risk_pct",
            "max_drawdown_pct",
        ):
            _finite_non_negative(getattr(self, name), name)
        if (
            not isinstance(self.max_switches_per_hour, int)
            or isinstance(self.max_switches_per_hour, bool)
            or self.max_switches_per_hour < 0
        ):
            raise ValueError("max_switches_per_hour must be a non-negative integer")
        if self.min_liquidity_score > 10:
            raise ValueError("min_liquidity_score cannot exceed 10")


@dataclass(frozen=True, slots=True)
class IncumbentPosition:
    """Current position's forward-looking, fee-adjusted hold thesis."""

    symbol: str
    side: str
    remaining_net_edge_pct: float
    hold_seconds: float
    stagnation_score: float
    thesis_health: str = "intact"
    exit_fee_bps: float = 0.0
    exit_slippage_bps: float = 0.0

    def __post_init__(self) -> None:
        if not self.symbol or not self.symbol.strip():
            raise ValueError("incumbent symbol is required")
        if self.side.upper() not in {"LONG", "SHORT"}:
            raise ValueError("incumbent side must be LONG or SHORT")
        if not isfinite(float(self.remaining_net_edge_pct)):
            raise ValueError("remaining_net_edge_pct must be finite")
        _finite_non_negative(self.hold_seconds, "hold_seconds")
        if (
            not isfinite(self.stagnation_score)
            or not 0 <= self.stagnation_score <= 1
        ):
            raise ValueError("stagnation_score must be between 0 and 1")
        if self.thesis_health.lower() not in {"intact", "degraded", "invalidated"}:
            raise ValueError("thesis_health must be intact, degraded, or invalidated")
        _finite_non_negative(self.exit_fee_bps, "exit_fee_bps")
        _finite_non_negative(self.exit_slippage_bps, "exit_slippage_bps")


@dataclass(frozen=True, slots=True)
class RebalanceCandidate:
    """Replacement opportunity estimate after entry/exit execution costs."""

    symbol: str
    side: str
    expected_gross_return_pct: float
    expected_loss_pct: float
    loss_probability: float
    fee_bps: float
    slippage_bps: float
    funding_bps: float
    confidence: float
    liquidity_score: float
    risk_pct: float
    liquidation_distance_pct: float

    def __post_init__(self) -> None:
        if not self.symbol or not self.symbol.strip():
            raise ValueError("candidate symbol is required")
        if self.side.upper() not in {"LONG", "SHORT"}:
            raise ValueError("candidate side must be LONG or SHORT")
        for name in (
            "expected_gross_return_pct",
            "expected_loss_pct",
            "fee_bps",
            "slippage_bps",
            "funding_bps",
            "risk_pct",
            "liquidation_distance_pct",
        ):
            _finite_non_negative(getattr(self, name), name)
        for name in ("loss_probability", "confidence"):
            value = float(getattr(self, name))
            if not isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if (
            not isfinite(self.liquidity_score)
            or not 0 <= self.liquidity_score <= 10
        ):
            raise ValueError("liquidity_score must be between 0 and 10")

    @property
    def total_cost_pct(self) -> float:
        return (self.fee_bps + self.slippage_bps + self.funding_bps) * _BPS_TO_PCT

    @property
    def expected_net_edge_pct(self) -> float:
        return (
            self.expected_gross_return_pct
            - self.total_cost_pct
            - self.loss_probability * self.expected_loss_pct
        )


@dataclass(frozen=True, slots=True)
class RebalanceContext:
    """Portfolio state required to authorize a proposed replacement."""

    portfolio_risk_after_incumbent_close_pct: float
    candidate_correlated_incremental_risk_pct: float
    drawdown_pct: float
    available_capital_after_incumbent_close_usd: float
    candidate_capital_usd: float
    seconds_since_last_switch: float = float("inf")
    switches_last_hour: int = 0

    def __post_init__(self) -> None:
        for name in (
            "portfolio_risk_after_incumbent_close_pct",
            "candidate_correlated_incremental_risk_pct",
            "drawdown_pct",
            "available_capital_after_incumbent_close_usd",
            "candidate_capital_usd",
            "seconds_since_last_switch",
        ):
            value = float(getattr(self, name))
            if name == "seconds_since_last_switch" and value == float("inf"):
                continue
            _finite_non_negative(value, name)
        if (
            not isinstance(self.switches_last_hour, int)
            or isinstance(self.switches_last_hour, bool)
            or self.switches_last_hour < 0
        ):
            raise ValueError("switches_last_hour must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class RebalanceDecision:
    should_switch: bool
    incumbent_symbol: str
    candidate_symbol: str
    candidate_net_edge_pct: float
    incumbent_hold_edge_pct: float
    incumbent_exit_cost_pct: float
    net_advantage_pct: float
    reasons: tuple[str, ...]


class DynamicRebalancer:
    """Compare a replacement with the incumbent without weakening hard limits."""

    def __init__(self, config: RebalanceConfig | None = None) -> None:
        self.config = config or RebalanceConfig()

    def evaluate(
        self,
        incumbent: IncumbentPosition,
        candidate: RebalanceCandidate,
        context: RebalanceContext,
    ) -> RebalanceDecision:
        cfg = self.config
        exit_cost = (
            incumbent.exit_fee_bps + incumbent.exit_slippage_bps
        ) * _BPS_TO_PCT
        candidate_edge = candidate.expected_net_edge_pct
        advantage = candidate_edge - incumbent.remaining_net_edge_pct - exit_cost
        reasons: list[str] = []

        if candidate.symbol.strip().upper() == incumbent.symbol.strip().upper():
            reasons.append("same_instrument")
        if candidate_edge <= 0:
            reasons.append("candidate_net_edge_non_positive")
        if candidate.loss_probability > cfg.max_candidate_loss_probability:
            reasons.append("candidate_loss_probability_above_limit")
        if candidate.confidence < cfg.min_candidate_confidence:
            reasons.append("candidate_confidence_below_threshold")
        if candidate.liquidity_score < cfg.min_liquidity_score:
            reasons.append("candidate_liquidity_below_minimum")
        if candidate.liquidation_distance_pct < cfg.min_liquidation_distance_pct:
            reasons.append("candidate_liquidation_buffer_too_small")
        if candidate.risk_pct <= 0 or candidate.risk_pct > cfg.max_trade_risk_pct:
            reasons.append("candidate_trade_risk_out_of_bounds")
        if (
            context.portfolio_risk_after_incumbent_close_pct
            + context.candidate_correlated_incremental_risk_pct
            > cfg.max_portfolio_risk_pct
        ):
            reasons.append("portfolio_risk_limit")
        if context.drawdown_pct >= cfg.max_drawdown_pct:
            reasons.append("drawdown_protection_stop")
        if (
            context.candidate_capital_usd
            > context.available_capital_after_incumbent_close_usd
        ):
            reasons.append("insufficient_available_capital")
        if context.seconds_since_last_switch < cfg.switch_cooldown_seconds:
            reasons.append("switch_cooldown_active")
        if context.switches_last_hour >= cfg.max_switches_per_hour:
            reasons.append("hourly_switch_limit")
        if incumbent.hold_seconds < cfg.min_hold_seconds:
            reasons.append("minimum_hold_time_not_met")

        incumbent_weak = (
            incumbent.thesis_health.lower() in {"degraded", "invalidated"}
            or incumbent.stagnation_score >= cfg.min_incumbent_stagnation
        )
        if not incumbent_weak:
            reasons.append("incumbent_thesis_or_momentum_still_healthy")
        if advantage < cfg.min_net_advantage_pct:
            reasons.append("net_advantage_below_switch_threshold")

        return RebalanceDecision(
            should_switch=not reasons,
            incumbent_symbol=incumbent.symbol,
            candidate_symbol=candidate.symbol,
            candidate_net_edge_pct=round(candidate_edge, 8),
            incumbent_hold_edge_pct=round(incumbent.remaining_net_edge_pct, 8),
            incumbent_exit_cost_pct=round(exit_cost, 8),
            net_advantage_pct=round(advantage, 8),
            reasons=tuple(reasons) if reasons else ("high_conviction_net_advantage",),
        )
