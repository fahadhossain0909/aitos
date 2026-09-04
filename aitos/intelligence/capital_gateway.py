"""Bridge strategy opportunities into the capital objective and protection gate."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from aitos.intelligence.capital_controls import (
    CapitalCircuitBreaker,
    CapitalControlConfig,
    ProbabilityCalibrator,
    execution_cost_bps,
    opportunity_age_seconds,
)
from aitos.intelligence.capital_objective import (
    CapitalAllocation,
    CapitalAllocator,
    CapitalDecision,
    CapitalObjective,
    OpportunityEstimate,
)
from aitos.intelligence.capital_protection import (
    CapitalReservation,
    PortfolioProtection,
    PortfolioRiskSnapshot,
    Reservation,
)
from aitos.models.trade import Opportunity, TradeSide


@dataclass(frozen=True)
class CapitalGatewayResult:
    decisions: tuple[CapitalDecision, ...]
    allocations: tuple[CapitalAllocation, ...]

    def allocation_for(self, symbol: str) -> CapitalAllocation | None:
        return next((a for a in self.allocations if a.symbol == symbol), None)


class CapitalGateway:
    """Single decision boundary between strategy evidence and capital use."""

    def __init__(
        self,
        objective: CapitalObjective | None = None,
        allocator: CapitalAllocator | None = None,
        protection: PortfolioProtection | None = None,
        reservation: CapitalReservation | None = None,
        controls: CapitalControlConfig | None = None,
        calibrator: ProbabilityCalibrator | None = None,
    ) -> None:
        self.objective = objective or CapitalObjective()
        self.allocator = allocator or CapitalAllocator(self.objective)
        self.protection = protection or PortfolioProtection()
        self.reservation = reservation or CapitalReservation()
        self.controls = controls or CapitalControlConfig()
        self.circuit_breaker = CapitalCircuitBreaker(self.controls)
        self.calibrator = calibrator or ProbabilityCalibrator(self.controls)

    def estimate_opportunity(
        self,
        opportunity: Opportunity,
        *,
        fee_bps: float = 10.0,
        slippage_bps: float = 5.0,
        funding_bps: float = 0.0,
    ) -> OpportunityEstimate:
        """Build a conservative economic estimate using current execution conditions."""
        entry = float(opportunity.entry_price)
        if not isfinite(entry) or entry <= 0:
            raise ValueError("opportunity entry_price must be finite and positive")
        if not opportunity.take_profit_levels:
            raise ValueError("opportunity requires at least one take-profit level")
        tp = float(opportunity.take_profit_levels[0])
        stop = float(opportunity.stop_loss_price)
        if not isfinite(tp) or not isfinite(stop) or tp <= 0 or stop <= 0:
            raise ValueError("opportunity TP/SL prices must be finite and positive")

        if opportunity.side == TradeSide.LONG:
            gross = (tp - entry) / entry * 100.0
        else:
            gross = (entry - tp) / entry * 100.0
        expected_loss = abs(entry - stop) / entry * 100.0
        consensus: dict[str, Any] = opportunity.agent_consensus
        raw_probability = consensus.get("loss_probability")
        if raw_probability is None:
            raw_probability = 1.0 - float(opportunity.confidence)
        probability = self.calibrator.calibrate(float(raw_probability))
        liquidity = max(0.0, min(10.0, float(consensus.get("liquidity_score", 5.0))))
        volatility = consensus.get("volatility_score")
        effective_slippage = execution_cost_bps(
            base_fee_bps=fee_bps,
            base_slippage_bps=slippage_bps,
            liquidity_score=liquidity,
            volatility_score=volatility,
            config=self.controls,
        ) - max(0.0, float(fee_bps))
        return OpportunityEstimate(
            symbol=opportunity.symbol,
            expected_gross_return_pct=max(0.0, gross),
            expected_loss_pct=max(0.0, expected_loss),
            loss_probability=max(0.0, min(1.0, probability)),
            fee_bps=max(0.0, float(fee_bps)),
            slippage_bps=max(0.0, effective_slippage),
            funding_bps=max(0.0, float(funding_bps)),
            liquidity_score=liquidity,
            confidence=max(0.0, min(1.0, float(opportunity.confidence))),
            regime_fit=max(0.0, min(10.0, float(consensus.get("regime_fit_score", 5.0)))),
            metadata={
                "opportunity_id": opportunity.opportunity_id,
                "detected_at": opportunity.detected_at,
                "regime": opportunity.regime or consensus.get("runtime_regime"),
                "volatility_score": volatility,
                "equity_peak_usd": consensus.get("equity_peak_usd"),
                "position_risk_pct": consensus.get("position_risk_pct", {}),
                "correlations": consensus.get("correlations", {}),
                "daily_pnl_pct": consensus.get("daily_pnl_pct", 0.0),
                "consecutive_losses": consensus.get("consecutive_losses", 0),
            },
        )

    @staticmethod
    def _snapshot(equity_usd: float, estimate: OpportunityEstimate) -> PortfolioRiskSnapshot:
        metadata = estimate.metadata
        peak = float(metadata.get("equity_peak_usd") or equity_usd)
        positions = {str(k): float(v) for k, v in dict(metadata.get("position_risk_pct") or {}).items()}
        correlations: dict[tuple[str, str], float] = {}
        for key, value in dict(metadata.get("correlations") or {}).items():
            if isinstance(key, (tuple, list)) and len(key) == 2:
                correlations[(str(key[0]), str(key[1]))] = float(value)
            elif isinstance(key, str) and ":" in key:
                a, b = key.split(":", 1)
                correlations[(a, b)] = float(value)
        return PortfolioRiskSnapshot(equity_usd, peak, positions, correlations)

    def evaluate(
        self,
        equity_usd: float,
        estimates: list[OpportunityEstimate],
        *,
        max_positions: int = 3,
    ) -> CapitalGatewayResult:
        decisions: list[CapitalDecision] = []
        eligible_estimates: list[OpportunityEstimate] = []
        for estimate in estimates:
            metadata = estimate.metadata
            age = opportunity_age_seconds(str(metadata.get("detected_at", "")))
            if age > self.controls.opportunity_max_age_seconds:
                decisions.append(
                    CapitalDecision(
                        estimate.symbol, False, 0.0, 0.0, 0.0,
                        round(estimate.expected_net_edge_pct, 6),
                        round(estimate.total_cost_pct, 6),
                        ("opportunity_expired",),
                        (f"age_seconds={age:.3f}",),
                    )
                )
                continue
            allowed, breaker_reason = self.circuit_breaker.check(
                daily_pnl_pct=float(metadata.get("daily_pnl_pct", 0.0)),
                consecutive_losses=int(metadata.get("consecutive_losses", 0)),
            )
            if not allowed:
                decisions.append(
                    CapitalDecision(
                        estimate.symbol, False, 0.0, 0.0, 0.0,
                        round(estimate.expected_net_edge_pct, 6),
                        round(estimate.total_cost_pct, 6),
                        (breaker_reason,),
                        (),
                    )
                )
                continue
            eligible_estimates.append(estimate)
        ranked = self.objective.rank(eligible_estimates)
        decisions.extend(ranked)
        allocations = self.allocator.allocate(equity_usd, ranked, max_positions=max_positions)
        protected: list[CapitalAllocation] = []
        by_symbol = {item.symbol: item for item in eligible_estimates}
        for allocation in allocations:
            estimate = by_symbol[allocation.symbol]
            snapshot = self._snapshot(equity_usd, estimate)
            metadata = estimate.metadata
            requested_pct = allocation.risk_budget_usd / equity_usd * 100.0
            protection = self.protection.evaluate(
                symbol=allocation.symbol,
                requested_risk_pct=requested_pct,
                snapshot=snapshot,
                regime=str(metadata.get("regime") or ""),
                volatility_score=metadata.get("volatility_score"),
            )
            if not protection.allowed:
                continue
            risk_budget = equity_usd * protection.allowed_risk_pct / 100.0
            capital = risk_budget / max(self.objective.config.max_trade_risk_pct / 100.0, 1e-9)
            candidate = CapitalAllocation(
                allocation.symbol, round(capital, 8), round(risk_budget, 8), allocation.score
            )
            available_capital = max(0.0, equity_usd - self.reservation.reserved_capital_usd)
            available_risk = max(
                0.0,
                equity_usd * self.objective.config.max_portfolio_risk_pct / 100.0
                - self.reservation.reserved_risk_usd,
            )
            if self.reservation.reserve(
                Reservation(candidate.symbol, candidate.capital_usd, candidate.risk_budget_usd),
                available_capital_usd=available_capital,
                available_risk_usd=available_risk,
            ):
                protected.append(candidate)
        return CapitalGatewayResult(tuple(decisions), tuple(protected))

    def authorize_opportunity(
        self,
        equity_usd: float,
        opportunity: Opportunity,
        *,
        fee_bps: float = 10.0,
        slippage_bps: float = 5.0,
        funding_bps: float = 0.0,
    ) -> tuple[CapitalDecision, CapitalAllocation | None]:
        estimate = self.estimate_opportunity(
            opportunity, fee_bps=fee_bps, slippage_bps=slippage_bps, funding_bps=funding_bps
        )
        result = self.evaluate(equity_usd, [estimate], max_positions=1)
        decision = next((d for d in result.decisions if d.symbol == opportunity.symbol), None)
        if decision is None:
            decision = self.objective.evaluate(estimate)
        return decision, result.allocation_for(opportunity.symbol)

    def release(self, symbol: str) -> Reservation | None:
        """Release a reservation after cancellation/close."""
        return self.reservation.release(symbol)
