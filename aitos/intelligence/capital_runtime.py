"""Runtime enforcement for the capital-growth objective.

The guard is installed when the intelligence package is imported. It protects
the TradeLifecycle boundary itself so a caller cannot bypass the capital gate
by skipping the scanner/application helper.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from functools import wraps
from typing import Any

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
        tp_price=(opportunity.take_profit_levels[0] if opportunity.take_profit_levels else opportunity.entry_price),
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


def _portfolio_consensus(portfolio: Any, opportunity: Opportunity) -> dict[str, Any]:
    """Normalize portfolio state for the final capital boundary."""
    consensus = dict(opportunity.agent_consensus)
    if hasattr(portfolio, "peak_equity_usd"):
        consensus["equity_peak_usd"] = float(portfolio.peak_equity_usd)
    if hasattr(portfolio, "regime"):
        consensus["runtime_regime"] = str(portfolio.regime)
    if hasattr(portfolio, "volatility_percentile"):
        consensus["volatility_score"] = max(0.0, min(1.0, float(portfolio.volatility_percentile) / 100.0))
    if hasattr(portfolio, "daily_pnl_pct"):
        consensus["daily_pnl_pct"] = float(portfolio.daily_pnl_pct)
    # A lifecycle/portfolio integration may expose a live loss streak. Missing
    # telemetry is intentionally zero here; the existing risk engine remains a
    # separate hard safety layer for daily/weekly loss limits.
    if hasattr(portfolio, "consecutive_losses"):
        consensus["consecutive_losses"] = int(portfolio.consecutive_losses)
    positions = getattr(portfolio, "positions", ()) or ()
    if "position_risk_pct" not in consensus:
        consensus["position_risk_pct"] = {
            str(getattr(position, "symbol", "")): 1.0
            for position in positions
            if getattr(position, "symbol", "")
        }
    if "correlations" not in consensus and positions:
        pairwise = getattr(portfolio, "max_pairwise_correlation", None)
        if pairwise is not None and float(pairwise) > 0.0:
            consensus["correlations"] = {
                f"{getattr(position, 'symbol', '')}:{opportunity.symbol}": float(pairwise)
                for position in positions
                if getattr(position, "symbol", "")
            }
    return consensus


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
            return _rejected_trade(opportunity, "capital_objective: invalid or unavailable equity")

        protected_opportunity = replace(
            opportunity,
            agent_consensus=_portfolio_consensus(portfolio, opportunity),
        )
        try:
            decision, allocation = gateway.authorize_opportunity(equity, protected_opportunity)
        except (TypeError, ValueError, ArithmeticError) as exc:
            return _rejected_trade(protected_opportunity, f"capital_objective: {exc}")

        if not decision.eligible or allocation is None:
            return _rejected_trade(protected_opportunity, _capital_reason(decision))

        consensus = dict(protected_opportunity.agent_consensus)
        consensus["capital_objective"] = {
            "eligible": True,
            "composite_score": decision.composite_score,
            "expected_net_edge_pct": decision.expected_net_edge_pct,
            "risk_budget_usd": allocation.risk_budget_usd,
            "capital_usd": allocation.capital_usd,
            "risk_budget_pct": (allocation.risk_budget_usd / equity) * 100.0,
        }
        authorized = replace(protected_opportunity, agent_consensus=consensus)
        trade = await original(self, authorized, portfolio, *args, **kwargs)
        if trade.state == TradeLifecycleState.REJECTED:
            gateway.release(opportunity.symbol)
        return trade

    TradeLifecycle.submit_opportunity = guarded_submit  # type: ignore[method-assign]


install_capital_guard()
