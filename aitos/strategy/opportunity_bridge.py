"""Risk-governed, shadow-only StrategyEngine to canonical Opportunity bridge.

This adapter emits canonical AITOS Opportunity values for evaluation and UI/
research consumers. It never calls TradeLifecycle, creates an order, reserves
live capital, or grants production approval. A caller must supply a fresh,
isolated CapitalGateway factory so shadow evaluation cannot mutate live
reservations.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from math import isfinite
from typing import Callable, Mapping

from aitos.intelligence.capital_gateway import CapitalGateway
from aitos.models.trade import Opportunity, TradeSide

from .contracts import StrategyContext, StrategyMode
from .engine import StrategyCycle
from .shadow_bridge import build_strategy_shadow_report


@dataclass(frozen=True)
class StrategyProtectionPlan:
    """Explicit strategy-side protection; the bridge never invents stop/TPs."""

    stop_loss_price: float
    take_profit_levels: tuple[float, ...]
    loss_probability: float
    leverage: float = 1.0
    fee_bps: float = 10.0
    slippage_bps: float = 5.0
    funding_bps: float = 0.0


@dataclass(frozen=True)
class OpportunityRiskReceipt:
    opportunity_id: str
    symbol: str
    side: str
    capital_usd: float
    risk_budget_usd: float
    expected_net_edge_pct: float
    rejection_reasons: tuple[str, ...]
    rationale: tuple[str, ...]
    evaluated_at_ns: int
    execution_authorized: bool = False


@dataclass(frozen=True)
class GovernedOpportunity:
    opportunity: Opportunity
    risk_receipt: OpportunityRiskReceipt
    execution_eligible: bool = False


@dataclass(frozen=True)
class StrategyOpportunityBridgeReport:
    generated_at_ns: int
    opportunities: tuple[GovernedOpportunity, ...]
    rejected: tuple[tuple[str, str], ...]
    execution_eligible: bool = False


def _validate_protection(
    side: TradeSide, entry: float, plan: StrategyProtectionPlan
) -> str | None:
    if not plan.take_profit_levels:
        return "explicit take-profit levels are required"
    raw_values = (
        plan.stop_loss_price,
        plan.leverage,
        plan.fee_bps,
        plan.slippage_bps,
        plan.funding_bps,
        plan.loss_probability,
        *plan.take_profit_levels,
    )
    if any(isinstance(value, bool) for value in raw_values):
        return "protection plan values must be numeric, not boolean"
    try:
        stop, leverage, fees, slippage, funding, loss_probability, *targets = (
            float(value) for value in raw_values
        )
    except (TypeError, ValueError, OverflowError):
        return "protection plan contains non-numeric values"
    if not all(isfinite(value) for value in (stop, leverage, fees, slippage, funding, *targets)):
        return "protection plan contains non-finite values"
    if stop <= 0 or leverage < 1:
        return "stop and leverage must be positive (leverage >= 1)"
    if fees < 0 or slippage < 0:
        return "fee and slippage assumptions cannot be negative"
    if not 0.0 <= loss_probability <= 1.0:
        return "explicit loss probability must be within [0, 1]"
    if side is TradeSide.LONG:
        if not stop < entry:
            return "LONG stop must be below entry"
        if any(price <= entry for price in targets):
            return "LONG take-profit levels must be above entry"
    else:
        if not stop > entry:
            return "SHORT stop must be above entry"
        if any(price >= entry for price in targets):
            return "SHORT take-profit levels must be below entry"
    if any(price <= 0 for price in targets):
        return "take-profit levels must be positive"
    return None


def build_risk_governed_opportunities(
    cycle: StrategyCycle,
    context: StrategyContext,
    protection_plans: Mapping[str, StrategyProtectionPlan],
    *,
    shadow_gateway_factory: Callable[[], CapitalGateway],
    equity_usd: float,
    max_positions: int = 3,
    live_reservation: object,
) -> StrategyOpportunityBridgeReport:
    """Produce canonical Opportunity objects after isolated capital/risk review.

    shadow_gateway_factory must return a fresh gateway whose reservation
    ledger is isolated from the live runtime. The output is always marked
    non-production and execution-ineligible, including accepted candidates.
    """
    if context.mode is not StrategyMode.SHADOW:
        raise ValueError("canonical strategy bridge only accepts SHADOW context")
    if context.now_ns <= 0:
        raise ValueError("point-in-time strategy timestamp is required")
    if not isfinite(equity_usd) or equity_usd <= 0:
        raise ValueError("shadow portfolio equity must be finite and positive")
    if not callable(shadow_gateway_factory):
        raise TypeError("isolated shadow CapitalGateway factory is required")
    if live_reservation is None:
        raise ValueError("live reservation identity is required for isolation")

    gateway = shadow_gateway_factory()
    if not isinstance(gateway, CapitalGateway):
        raise TypeError("shadow gateway factory must return CapitalGateway")
    if live_reservation is not None and gateway.reservation is live_reservation:
        raise ValueError("shadow gateway cannot share the live reservation ledger")

    shadow = build_strategy_shadow_report(cycle, context)
    allocations = {item.strategy_id: item for item in cycle.allocations}
    results = {item.strategy_id: item for item in cycle.results}
    candidates: list[tuple[str, Opportunity, object, float]] = []
    rejected = [(key, reason) for key, reason in shadow.rejected]
    seen_symbols: set[str] = set()
    seen_strategies: set[str] = set()
    estimates = []

    for proposal in shadow.proposals:
        key = f"{proposal.strategy_id}:{proposal.instrument_id}"
        result = results.get(proposal.strategy_id)
        allocation = allocations.get(proposal.strategy_id)
        if proposal.strategy_id in seen_strategies:
            rejected.append(
                (key, "multiple intents require explicit per-instrument capital allocation")
            )
            continue
        seen_strategies.add(proposal.strategy_id)
        if result is None or result.capital_request is None or allocation is None:
            rejected.append((key, "typed capital request/allocation is missing"))
            continue
        snapshot = context.snapshots[proposal.instrument_id]
        symbol = proposal.instrument_id.rsplit(":", 1)[-1].upper()
        if (
            not symbol
            or not symbol.isalnum()
            or len(symbol) > 30
            or symbol in seen_symbols
        ):
            rejected.append((key, "duplicate or invalid canonical symbol"))
            continue
        seen_symbols.add(symbol)
        plan = protection_plans.get(proposal.instrument_id)
        if not isinstance(plan, StrategyProtectionPlan):
            rejected.append((key, "explicit typed protection plan is missing"))
            continue
        side = TradeSide.LONG if proposal.side == "LONG" else TradeSide.SHORT
        invalid = _validate_protection(side, proposal.reference_price, plan)
        if invalid:
            rejected.append((key, invalid))
            continue
        try:
            confidence = float(result.capital_request.confidence)
        except (TypeError, ValueError, OverflowError):
            rejected.append((key, "strategy confidence must be numeric"))
            continue
        if not isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            rejected.append((key, "strategy confidence must be within [0, 1]"))
            continue
        allocated_notional = min(proposal.allocated_notional, allocation.notional)
        if allocated_notional <= 0 or allocation.projected_loss <= 0:
            rejected.append((key, "strategy allocation and projected loss must be positive"))
            continue
        try:
            detected_at = datetime.fromtimestamp(
                context.now_ns / 1_000_000_000, tz=timezone.utc
            ).isoformat()
        except (OverflowError, OSError, ValueError):
            rejected.append((key, "strategy timestamp is outside supported range"))
            continue
        normalized_plan = StrategyProtectionPlan(
            stop_loss_price=float(plan.stop_loss_price),
            take_profit_levels=tuple(float(value) for value in plan.take_profit_levels),
            leverage=float(plan.leverage),
            fee_bps=float(plan.fee_bps),
            slippage_bps=float(plan.slippage_bps),
            funding_bps=float(plan.funding_bps),
            loss_probability=float(plan.loss_probability),
        )
        try:
            liquidity_score = float(snapshot.liquidity_score)
        except (TypeError, ValueError, OverflowError):
            rejected.append((key, "market liquidity score is invalid"))
            continue
        if not isfinite(liquidity_score):
            rejected.append((key, "market liquidity score is non-finite"))
            continue
        consensus = {
            "loss_probability": normalized_plan.loss_probability,
            "liquidity_score": liquidity_score,
            "leverage": normalized_plan.leverage,
            "side": side.value,
            "runtime_regime": context.global_regime,
            "position_risk_pct": dict(context.metadata.get("position_risk_pct", {})),
            "position_sides": dict(context.metadata.get("position_sides", {})),
            "position_instruments": dict(context.metadata.get("position_instruments", {})),
            "pending_order_risk_pct": dict(context.metadata.get("pending_order_risk_pct", {})),
            "pending_order_symbols": dict(context.metadata.get("pending_order_symbols", {})),
            "pending_order_sides": dict(context.metadata.get("pending_order_sides", {})),
            "correlations": dict(context.metadata.get("correlations", {})),
            "equity_peak_usd": context.metadata.get("equity_peak_usd", equity_usd),
            "daily_pnl_pct": context.metadata.get("daily_pnl_pct", 0.0),
            "consecutive_losses": context.metadata.get("consecutive_losses", 0),
            "liquidation_buffer_pct": context.metadata.get("liquidation_buffer_pct"),
            "decision_at": detected_at,
            "strategy_instrument_id": proposal.instrument_id,
            "strategy_requested_notional": proposal.requested_notional,
            "strategy_allocated_notional": allocated_notional,
            "strategy_projected_loss_usd": proposal.projected_loss,
            "shadow_only": True,
        }
        opportunity = Opportunity(
            symbol=symbol,
            side=side,
            entry_price=proposal.reference_price,
            stop_loss_price=normalized_plan.stop_loss_price,
            take_profit_levels=list(normalized_plan.take_profit_levels),
            confidence=confidence,
            strategy_id=proposal.strategy_id,
            rationale=proposal.rationale,
            agent_consensus=consensus,
            is_production=False,
            approved_by=None,
            regime=context.global_regime,
            detected_at=detected_at,
        )
        try:
            estimates.append(
                gateway.estimate_opportunity(
                    opportunity,
                    fee_bps=normalized_plan.fee_bps,
                    slippage_bps=normalized_plan.slippage_bps,
                    funding_bps=normalized_plan.funding_bps,
                )
            )
            candidates.append((key, opportunity, allocation, allocated_notional))
        except (TypeError, ValueError) as exc:
            rejected.append((key, f"capital estimate rejected: {exc}"))

    if not estimates:
        return StrategyOpportunityBridgeReport(
            context.now_ns, (), tuple(rejected), execution_eligible=False
        )

    try:
        reviewed = gateway.evaluate(equity_usd, estimates, max_positions=max_positions)
    except Exception as exc:
        rejected.extend(
            (key, f"capital risk review failed closed: {type(exc).__name__}")
            for key, _opportunity, _allocation, _cap in candidates
        )
        return StrategyOpportunityBridgeReport(
            context.now_ns, (), tuple(rejected), execution_eligible=False
        )
    decisions = {item.symbol: item for item in reviewed.decisions}
    capital = {item.symbol: item for item in reviewed.allocations}
    governed: list[GovernedOpportunity] = []
    for key, opportunity, strategy_allocation, requested_capital_cap in candidates:
        decision = decisions.get(opportunity.symbol)
        approved = capital.get(opportunity.symbol)
        if decision is None or not decision.eligible or approved is None:
            reasons = decision.rejection_reasons if decision else ("risk_review_missing",)
            rejected.append((key, ";".join(reasons)))
            continue
        try:
            approved_capital = float(approved.capital_usd)
            approved_risk = float(approved.risk_budget_usd)
            strategy_capital = float(strategy_allocation.notional)
            strategy_risk_cap = float(strategy_allocation.projected_loss)
        except (TypeError, ValueError, OverflowError):
            rejected.append((key, "risk gateway returned malformed allocation values"))
            continue
        if not all(
            isfinite(value)
            for value in (
                approved_capital,
                approved_risk,
                strategy_capital,
                strategy_risk_cap,
            )
        ):
            rejected.append((key, "risk gateway returned non-finite allocation values"))
            continue
        amount = min(
            approved_capital,
            strategy_capital,
            float(requested_capital_cap),
        )
        if amount <= 0 or approved_capital <= 0 or approved_risk <= 0:
            rejected.append((key, "risk gateway returned zero capital or risk budget"))
            continue
        risk_budget = approved_risk * (amount / approved_capital)
        if strategy_risk_cap <= 0:
            rejected.append((key, "strategy projected-loss cap is invalid"))
            continue
        if risk_budget > strategy_risk_cap:
            scale = strategy_risk_cap / risk_budget
            amount *= scale
            risk_budget = strategy_risk_cap
        if amount <= 0: 
            rejected.append((key, "strategy risk cap reduced capital to zero"))
            continue
        receipt = OpportunityRiskReceipt(
            opportunity_id=opportunity.opportunity_id,
            symbol=opportunity.symbol,
            side=opportunity.side.value,
            capital_usd=amount,
            risk_budget_usd=risk_budget,
            expected_net_edge_pct=float(decision.expected_net_edge_pct),
            rejection_reasons=tuple(decision.rejection_reasons),
            rationale=tuple(decision.rationale),
            evaluated_at_ns=context.now_ns,
            execution_authorized=False,
        )
        opportunity = replace(
            opportunity,
            agent_consensus={
                **opportunity.agent_consensus,
                "capital_governance": {
                    "capital_usd": amount,
                    "risk_budget_usd": risk_budget,
                    "expected_net_edge_pct": receipt.expected_net_edge_pct,
                    "rationale": list(receipt.rationale),
                    "execution_authorized": False,
                },
            },
        )
        governed.append(GovernedOpportunity(opportunity, receipt, False))

    return StrategyOpportunityBridgeReport(
        generated_at_ns=context.now_ns,
        opportunities=tuple(governed),
        rejected=tuple(rejected),
        execution_eligible=False,
    )
