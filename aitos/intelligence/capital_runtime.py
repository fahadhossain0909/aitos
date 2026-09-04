"""Runtime enforcement for the capital-growth objective.

The guard is installed when the intelligence package is imported, which is
already part of the normal AITOS application bootstrap.  It protects the
TradeLifecycle boundary itself so a caller cannot bypass capital authorization
by skipping the scanner/application helper.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
from inspect import isawaitable
from typing import Any, Awaitable, Callable

from aitos.intelligence.capital_gateway import CapitalGateway
from aitos.models.trade import Opportunity, Trade, TradeLifecycleState
from aitos.trading.lifecycle import TradeLifecycle


_ORIGINAL_ATTR = "_aitos_capital_original_submit_opportunity"


def _rejected_trade(opportunity: Opportunity, reason: str) -> Trade:
    now = datetime.now(timezone.utc).isoformat()
    return Trade(
        trade_id=f"capital-reject-{opportunity.opportunity_id}",
        symbol=opportunity.symbol,
        side=opportunity.side,
        entry_price=opportunity.entry_price,
        quantity=0.0,
        leverage=1.0,
        position_size_usd=0.0,
        risk_amount_usd=0.0,
        strategy_id=opportunity.strategy_id,
        agent_consensus=dict(opportunity.agent_consensus),
        explanation=opportunity.rationale,
        sl_price=opportunity.stop_loss_price,
        tp_price=(
            opportunity.take_profit_levels[0]
            if opportunity.take_profit_levels
            else opportunity.entry_price
        ),
        state=TradeLifecycleState.REJECTED,
        entry_time=now,
        take_profit_levels=list(opportunity.take_profit_levels),
        regime=opportunity.regime,
        rejection_reason=reason,
    )


def _capital_reason(decision: Any) -> str:
    reasons = getattr(decision, "rejection_reasons", None)
    if reasons:
        return "capital_objective: " + "; ".join(str(item) for item in reasons)
    return "capital_objective: opportunity not allocated"


def install_capital_guard() -> None:
    """Install the capital gate exactly once on TradeLifecycle."""
    if hasattr(TradeLifecycle, _ORIGINAL_ATTR):
        return

    original = TradeLifecycle.submit_opportunity
    setattr(TradeLifecycle, _ORIGINAL_ATTR, original)
    gateway = CapitalGateway()

    @wraps(original)
    async def guarded_submit(
        self: TradeLifecycle,
        opportunity: Opportunity,
        portfolio: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Trade:
        equity = float(getattr(portfolio, "equity_usd", 0.0) or 0.0)
        if equity <= 0:
            reason = "capital_objective: invalid or unavailable equity"
            return _rejected_trade(opportunity, reason)

        try:
            decision, allocation = gateway.authorize_opportunity(
                equity, opportunity
            )
        except (TypeError, ValueError, ArithmeticError) as exc:
            return _rejected_trade(opportunity, f"capital_objective: {exc}")

        if not decision.eligible or allocation is None:
            return _rejected_trade(opportunity, _capital_reason(decision))

        # Preserve the approved allocation on the opportunity so downstream
        # lifecycle/risk code and journal consumers can audit the authorization.
        consensus = dict(opportunity.agent_consensus)
        consensus["capital_objective"] = {
            "eligible": True,
            "composite_score": decision.composite_score,
            "expected_net_edge_pct": decision.estimate.expected_net_edge_pct,
            "risk_budget_pct": allocation.risk_budget_pct,
            "risk_amount_usd": allocation.risk_amount_usd,
            "position_notional_usd": allocation.position_notional_usd,
        }
        from dataclasses import replace

        authorized = replace(opportunity, agent_consensus=consensus)
        return await original(self, authorized, portfolio, *args, **kwargs)

    TradeLifecycle.submit_opportunity = guarded_submit  # type: ignore[method-assign]


install_capital_guard()
