"""Capital-growth objective and asset ranking for AITOS.

The objective is deliberately independent from any venue or asset class. It
turns an opportunity estimate into a decision score that answers the actual
portfolio question: *where should capital be deployed now, if anywhere?*

Growth is optimized only after hard capital-protection constraints pass. All
costs are expressed in basis points so the same model works for crypto,
equities, FX, futures and other adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite

_BPS_TO_PCT = 0.01


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _finite_non_negative(value: float, name: str) -> float:
    if not isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return value


@dataclass(frozen=True)
class CapitalObjectiveConfig:
    """Policy knobs for sustainable capital compounding.

    ``max_*`` fields are hard vetoes, not soft score penalties. Expected loss
    is the adverse price move to the configured stop. The default 5% price
    excursion cap is intentionally separate from the 1% account-level
    ``max_trade_risk_pct`` cap enforced by the allocator; position sizing can
    therefore keep a wider market stop inside the portfolio risk budget.
    """

    growth_weight: float = 0.60
    protection_weight: float = 0.40
    min_net_edge_pct: float = 0.05
    min_expected_return_pct: float = 0.10
    max_loss_probability: float = 0.35
    max_expected_loss_pct: float = 5.00
    max_cost_pct: float = 0.50
    min_liquidity_score: float = 4.0
    target_net_edge_pct: float = 1.00
    max_trade_risk_pct: float = 1.00
    max_portfolio_risk_pct: float = 5.00

    def __post_init__(self) -> None:
        if self.growth_weight < 0 or self.protection_weight < 0:
            raise ValueError("objective weights must be non-negative")
        if self.growth_weight + self.protection_weight <= 0:
            raise ValueError("at least one objective weight must be positive")
        if not 0 <= self.max_loss_probability <= 1:
            raise ValueError("max_loss_probability must be between 0 and 1")
        for name in (
            "min_net_edge_pct",
            "min_expected_return_pct",
            "max_expected_loss_pct",
            "max_cost_pct",
            "min_liquidity_score",
            "target_net_edge_pct",
            "max_trade_risk_pct",
            "max_portfolio_risk_pct",
        ):
            _finite_non_negative(getattr(self, name), name)


@dataclass(frozen=True)
class OpportunityEstimate:
    """Normalized economic/risk estimate produced by strategy intelligence."""

    symbol: str
    expected_gross_return_pct: float
    expected_loss_pct: float
    loss_probability: float
    fee_bps: float
    slippage_bps: float
    funding_bps: float = 0.0
    liquidity_score: float = 10.0
    confidence: float = 0.5
    regime_fit: float = 5.0
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "expected_gross_return_pct",
            "expected_loss_pct",
            "fee_bps",
            "slippage_bps",
            "funding_bps",
        ):
            value = getattr(self, name)
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if not 0 <= self.loss_probability <= 1:
            raise ValueError("loss_probability must be between 0 and 1")
        if not 0 <= self.liquidity_score <= 10:
            raise ValueError("liquidity_score must be between 0 and 10")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if not 0 <= self.regime_fit <= 10:
            raise ValueError("regime_fit must be between 0 and 10")

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

    @property
    def expected_net_return_pct(self) -> float:
        return self.expected_gross_return_pct - self.total_cost_pct


@dataclass(frozen=True)
class CapitalDecision:
    symbol: str
    eligible: bool
    growth_score: float
    protection_score: float
    composite_score: float
    expected_net_edge_pct: float
    total_cost_pct: float
    rejection_reasons: tuple[str, ...] = ()
    rationale: tuple[str, ...] = ()


class CapitalObjective:
    """Evaluate opportunities against the capital-growth/protection mandate."""

    def __init__(self, config: CapitalObjectiveConfig | None = None) -> None:
        self.config = config or CapitalObjectiveConfig()

    def evaluate(self, estimate: OpportunityEstimate) -> CapitalDecision:
        cfg = self.config
        edge = estimate.expected_net_edge_pct
        cost = estimate.total_cost_pct
        reasons: list[str] = []
        if edge < cfg.min_net_edge_pct:
            reasons.append("net_edge_below_minimum")
        if estimate.expected_net_return_pct < cfg.min_expected_return_pct:
            reasons.append("expected_return_below_minimum")
        if estimate.loss_probability > cfg.max_loss_probability:
            reasons.append("loss_probability_above_limit")
        if estimate.expected_loss_pct > cfg.max_expected_loss_pct:
            reasons.append("expected_loss_above_limit")
        if cost > cfg.max_cost_pct:
            reasons.append("trading_cost_above_limit")
        if estimate.liquidity_score < cfg.min_liquidity_score:
            reasons.append("liquidity_below_minimum")

        growth_score = _clamp(100.0 * edge / max(cfg.target_net_edge_pct, 1e-9))
        probability_score = 100.0 * (1.0 - estimate.loss_probability)
        loss_score = 100.0 * (
            1.0
            - min(
                1.0, estimate.expected_loss_pct / max(cfg.max_expected_loss_pct, 1e-9)
            )
        )
        liquidity_score = estimate.liquidity_score * 10.0
        confidence_score = estimate.confidence * 100.0
        protection_score = _clamp(
            0.45 * probability_score
            + 0.25 * loss_score
            + 0.20 * liquidity_score
            + 0.10 * confidence_score
        )
        weight_total = cfg.growth_weight + cfg.protection_weight
        composite = (
            growth_score * cfg.growth_weight + protection_score * cfg.protection_weight
        ) / weight_total
        rationale = (
            f"gross_return={estimate.expected_gross_return_pct:.4f}%",
            f"expected_loss={estimate.expected_loss_pct:.4f}%@{estimate.loss_probability:.3f}",
            f"cost={cost:.4f}%",
            f"net_edge={edge:.4f}%",
            f"growth_score={growth_score:.2f}",
            f"protection_score={protection_score:.2f}",
            f"liquidity={estimate.liquidity_score:.2f}/10",
        )
        return CapitalDecision(
            symbol=estimate.symbol,
            eligible=not reasons,
            growth_score=round(growth_score, 4),
            protection_score=round(protection_score, 4),
            composite_score=round(composite, 4),
            expected_net_edge_pct=round(edge, 6),
            total_cost_pct=round(cost, 6),
            rejection_reasons=tuple(reasons),
            rationale=rationale,
        )

    def rank(
        self, estimates: list[OpportunityEstimate], limit: int | None = None
    ) -> list[CapitalDecision]:
        decisions = [self.evaluate(item) for item in estimates]
        decisions.sort(key=lambda item: item.composite_score, reverse=True)
        eligible = [item for item in decisions if item.eligible]
        return eligible[:limit] if limit is not None else eligible


@dataclass(frozen=True)
class CapitalAllocation:
    symbol: str
    capital_usd: float
    risk_budget_usd: float
    score: float


class CapitalAllocator:
    """Allocate a bounded risk budget across eligible opportunities."""

    def __init__(self, objective: CapitalObjective | None = None) -> None:
        self.objective = objective or CapitalObjective()

    def allocate(
        self,
        equity_usd: float,
        decisions: list[CapitalDecision],
        *,
        max_positions: int = 3,
    ) -> list[CapitalAllocation]:
        if not isfinite(equity_usd) or equity_usd <= 0:
            raise ValueError("equity_usd must be positive")
        selected = [d for d in decisions if d.eligible][:max_positions]
        if not selected:
            return []
        cfg = self.objective.config
        total_risk_usd = equity_usd * cfg.max_portfolio_risk_pct / 100.0
        per_trade_cap = equity_usd * cfg.max_trade_risk_pct / 100.0
        total_score = sum(max(0.0, d.composite_score) for d in selected)
        if total_score <= 0:
            return []
        allocations: list[CapitalAllocation] = []
        remaining_risk = total_risk_usd
        for index, decision in enumerate(selected):
            proportional = total_risk_usd * decision.composite_score / total_score
            risk = min(per_trade_cap, proportional, max(0.0, remaining_risk))
            if index == len(selected) - 1:
                risk = min(risk, max(0.0, remaining_risk))
            risk = round(risk, 8)
            risk = min(risk, round(max(0.0, remaining_risk), 8))
            capital = risk / max(cfg.max_trade_risk_pct / 100.0, 1e-9)
            allocations.append(
                CapitalAllocation(
                    symbol=decision.symbol,
                    capital_usd=round(capital, 8),
                    risk_budget_usd=risk,
                    score=decision.composite_score,
                )
            )
            remaining_risk = max(0.0, remaining_risk - risk)
        return allocations
