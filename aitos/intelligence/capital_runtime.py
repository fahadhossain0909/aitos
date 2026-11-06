"""Runtime enforcement for the capital-growth objective.

The guard is installed by aitos/trading/__init__.py once TradeLifecycle is
fully defined -- see the note below install_capital_guard() for why this
module doesn't self-install on import anymore.
It protects the TradeLifecycle boundary itself so a caller cannot bypass the
capital gate by skipping the scanner/application helper.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from functools import wraps
from threading import RLock
from typing import TYPE_CHECKING, Any

from aitos.config.paths import get_model_data_dir
from aitos.intelligence.capital_controls import ProbabilityCalibrator
from aitos.intelligence.capital_gateway import CapitalGateway
from aitos.intelligence.capital_protection import CapitalReservation
from aitos.intelligence.opportunity_outcomes import DirectionalFunnelStore
from aitos.models.trade import Opportunity, Trade, TradeLifecycleState

if TYPE_CHECKING:
    # Deferred to a function-local import in install_capital_guard() below:
    # aitos.trading.lifecycle -> aitos.kernel.ai_kernel ->
    # aitos.intelligence.contextual_decision -> aitos.intelligence
    # (this module's own package) -> aitos.intelligence.capital_runtime, so
    # importing TradeLifecycle at module level here closes a real import
    # cycle. It only actually failed for a few entry points (`import
    # aitos.trading`, `import aitos.kernel`, `import
    # aitos.trading.lifecycle` directly) because aitos.app / aitos.intelligence
    # happened to establish a safe order first -- but that made it fragile to
    # any future reordering, not fixed. `from __future__ import annotations`
    # above means the `self: TradeLifecycle` hint below never needed the
    # runtime binding anyway.
    from aitos.trading.lifecycle import TradeLifecycle

_ORIGINAL_ATTR = "_aitos_capital_original_submit_opportunity"
_GATEWAY_ATTR = "_aitos_capital_gateway"
_FUNNEL_ATTR = "_aitos_directional_funnel"
_FUNNEL_LOCK = RLock()


def _funnel_store(self: TradeLifecycle) -> DirectionalFunnelStore:
    with _FUNNEL_LOCK:
        store = getattr(self, _FUNNEL_ATTR, None)
        if store is None:
            store = DirectionalFunnelStore(
                get_model_data_dir() / "directional_funnel.sqlite3"
            )
            setattr(self, _FUNNEL_ATTR, store)
        return store


def _record_funnel_sync(
    self: TradeLifecycle,
    *,
    symbol: str,
    side: str,
    stage: str,
    accepted: bool,
    reason: str,
    details: dict[str, Any] | None = None,
) -> None:
    _funnel_store(self).record(
        symbol=symbol,
        side=side,
        stage=stage,
        accepted=accepted,
        reason=reason,
        details=details,
    )


async def _record_funnel(
    self: TradeLifecycle,
    *,
    symbol: str,
    side: str,
    stage: str,
    accepted: bool,
    reason: str,
    details: dict[str, Any] | None = None,
) -> None:
    try:
        await asyncio.to_thread(
            _record_funnel_sync,
            self,
            symbol=symbol,
            side=side,
            stage=stage,
            accepted=accepted,
            reason=reason,
            details=details,
        )
    except Exception:
        # The ledger is diagnostic only; it must never veto or delay a trade.
        return


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


def _portfolio_consensus(portfolio: Any, opportunity: Opportunity) -> dict[str, Any]:
    """Normalize portfolio state for the final capital boundary."""
    consensus = dict(opportunity.agent_consensus)
    if hasattr(portfolio, "peak_equity_usd"):
        consensus["equity_peak_usd"] = float(portfolio.peak_equity_usd)
    if hasattr(portfolio, "regime"):
        consensus["runtime_regime"] = str(portfolio.regime)
    if hasattr(portfolio, "volatility_percentile"):
        consensus["volatility_score"] = max(
            0.0, min(1.0, float(portfolio.volatility_percentile) / 100.0)
        )
    if hasattr(portfolio, "daily_pnl_pct"):
        consensus["daily_pnl_pct"] = float(portfolio.daily_pnl_pct)
    if hasattr(portfolio, "consecutive_losses"):
        consensus["consecutive_losses"] = int(portfolio.consecutive_losses)
    positions = getattr(portfolio, "positions", ()) or ()
    equity = float(getattr(portfolio, "equity_usd", 0.0) or 0.0)
    # The live portfolio is authoritative; scanner-supplied exposure snapshots
    # may be stale by the time the final lifecycle gate is reached.
    consensus["position_risk_pct"] = {}
    consensus["position_sides"] = {}
    for position in positions:
        symbol = str(getattr(position, "symbol", "") or "")
        if not symbol:
            continue
        risk_amount = getattr(position, "risk_amount_usd", None)
        if risk_amount is None or float(risk_amount) <= 0 or equity <= 0:
            risk_pct = 1.0
        else:
            risk_pct = float(risk_amount) / equity * 100.0
        consensus["position_risk_pct"][symbol] = risk_pct
        side = getattr(position, "side", None)
        if side is not None:
            consensus["position_sides"][symbol] = str(side).upper()
    if "correlations" not in consensus and positions:
        pairwise = getattr(portfolio, "max_pairwise_correlation", None)
        if pairwise is not None and float(pairwise) > 0.0:
            consensus["correlations"] = {
                f"{getattr(position, 'symbol', '')}:{opportunity.symbol}": float(
                    pairwise
                )
                for position in positions
                if getattr(position, "symbol", "")
            }
    return consensus


async def _hard_limit_reason(self: TradeLifecycle, portfolio: Any) -> str | None:
    """Return a hard-cap reason while preserving RiskEngine emergency effects."""
    risk_engine = getattr(self, "_risk_engine", None)
    if risk_engine is None:
        return None
    try:
        breaches = risk_engine.check_limits(portfolio)
    except Exception:
        return None
    breach = next(
        (item for item in breaches if getattr(item, "is_hard_cap", False)), None
    )
    if breach is None:
        return None
    try:
        await risk_engine.assess(portfolio)
    except Exception:
        pass
    return f"hard limit breach: {breach.message}"


def install_capital_guard() -> None:
    """Install the capital gate exactly once on TradeLifecycle."""
    from aitos.trading.lifecycle import TradeLifecycle

    if hasattr(TradeLifecycle, _ORIGINAL_ATTR):
        return

    original = TradeLifecycle.submit_opportunity
    setattr(TradeLifecycle, _ORIGINAL_ATTR, original)

    @wraps(original)
    async def guarded_submit(
        self: TradeLifecycle,
        opportunity: Opportunity,
        portfolio: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Trade:
        hard_limit_reason = await _hard_limit_reason(self, portfolio)
        if hard_limit_reason is not None:
            await _record_funnel(
                self,
                symbol=opportunity.symbol,
                side=opportunity.side.value,
                stage="risk_gate",
                accepted=False,
                reason=hard_limit_reason,
            )
            return _rejected_trade(opportunity, hard_limit_reason)
        await _record_funnel(
            self,
            symbol=opportunity.symbol,
            side=opportunity.side.value,
            stage="risk_gate",
            accepted=True,
            reason="hard_limits_clear",
        )

        equity = float(getattr(portfolio, "equity_usd", 0.0) or 0.0)
        if equity <= 0:
            reason = "capital_objective: invalid or unavailable equity"
            await _record_funnel(
                self,
                symbol=opportunity.symbol,
                side=opportunity.side.value,
                stage="capital_gate",
                accepted=False,
                reason=reason,
            )
            return _rejected_trade(opportunity, reason)

        protected_opportunity = replace(
            opportunity,
            agent_consensus=_portfolio_consensus(portfolio, opportunity),
        )
        gateway = getattr(self, _GATEWAY_ATTR, None)
        if gateway is None:
            gateway = CapitalGateway(
                reservation=CapitalReservation(
                    path=get_model_data_dir() / "capital_reservations.sqlite3"
                ),
                calibrator=ProbabilityCalibrator(
                    path=get_model_data_dir()
                    / "capital_probability_calibration.sqlite3"
                ),
            )
            setattr(self, _GATEWAY_ATTR, gateway)

        try:
            execution_costs = dict(
                protected_opportunity.agent_consensus.get("execution_costs") or {}
            )
            decision, allocation = gateway.authorize_opportunity(
                equity,
                protected_opportunity,
                fee_bps=float(execution_costs.get("fee_bps", 10.0)),
                slippage_bps=float(execution_costs.get("slippage_bps", 5.0)),
                funding_bps=float(execution_costs.get("funding_bps", 0.0)),
            )
        except (TypeError, ValueError, ArithmeticError) as exc:
            reason = f"capital_objective: {exc}"
            await _record_funnel(
                self,
                symbol=opportunity.symbol,
                side=opportunity.side.value,
                stage="capital_gate",
                accepted=False,
                reason=reason,
            )
            return _rejected_trade(protected_opportunity, reason)

        if not decision.eligible or allocation is None:
            reason = _capital_reason(decision)
            await _record_funnel(
                self,
                symbol=opportunity.symbol,
                side=opportunity.side.value,
                stage="capital_gate",
                accepted=False,
                reason=reason,
                details={
                    "expected_net_edge_pct": decision.expected_net_edge_pct,
                    "total_cost_pct": decision.total_cost_pct,
                },
            )
            return _rejected_trade(protected_opportunity, reason)
        await _record_funnel(
            self,
            symbol=opportunity.symbol,
            side=opportunity.side.value,
            stage="capital_gate",
            accepted=True,
            reason="capital_authorized",
            details={
                "expected_net_edge_pct": decision.expected_net_edge_pct,
                "total_cost_pct": decision.total_cost_pct,
                "risk_budget_usd": allocation.risk_budget_usd,
            },
        )

        consensus = dict(protected_opportunity.agent_consensus)
        consecutive_losses = max(
            0, int(protected_opportunity.agent_consensus.get("consecutive_losses", 0))
        )
        risk_reduction_multiplier = (
            gateway.controls.risk_reduction_multiplier
            if consecutive_losses >= gateway.controls.risk_reduction_after_losses
            else 1.0
        )
        raw_loss_probability = protected_opportunity.agent_consensus.get(
            "loss_probability"
        )
        if raw_loss_probability is None:
            raw_loss_probability = 1.0 - float(protected_opportunity.confidence)
        predicted_loss_probability = gateway.calibrator.calibrate(
            float(raw_loss_probability)
        )
        consensus["capital_objective"] = {
            "eligible": True,
            "composite_score": decision.composite_score,
            "expected_net_edge_pct": decision.expected_net_edge_pct,
            "predicted_loss_probability": predicted_loss_probability,
            "risk_budget_usd": allocation.risk_budget_usd,
            "capital_usd": allocation.capital_usd,
            "risk_budget_pct": (allocation.risk_budget_usd / equity) * 100.0,
            "risk_reduction_multiplier": risk_reduction_multiplier,
        }
        authorized = replace(protected_opportunity, agent_consensus=consensus)
        try:
            trade = await original(self, authorized, portfolio, *args, **kwargs)
        except Exception:
            await _record_funnel(
                self,
                symbol=opportunity.symbol,
                side=opportunity.side.value,
                stage="lifecycle_open",
                accepted=False,
                reason="lifecycle_exception",
            )
            raise
        else:
            accepted = trade.state == TradeLifecycleState.POSITION_OPENED
            rejection_reason = str(trade.rejection_reason or trade.state.value)
            stage = (
                "risk_engine_gate"
                if "risk veto" in rejection_reason
                or "hard limit breach" in rejection_reason
                else (
                    "governance_gate"
                    if "governance denied" in rejection_reason
                    else "lifecycle_open"
                )
            )
            await _record_funnel(
                self,
                symbol=opportunity.symbol,
                side=opportunity.side.value,
                stage=stage,
                accepted=accepted,
                reason="position_opened" if accepted else rejection_reason,
                details={"trade_id": trade.trade_id},
            )
            return trade
        finally:
            # Reservations cover only the authorization/execution window. The
            # live portfolio is the source of truth after lifecycle processing.
            gateway.release(opportunity.symbol)

    TradeLifecycle.submit_opportunity = guarded_submit  # type: ignore[method-assign]


# NOT called eagerly here anymore -- see aitos/trading/__init__.py, which
# calls this once TradeLifecycle is fully defined. Calling it at this
# module's own import time was the other half of a real circular import
# (aitos.trading -> aitos.kernel -> aitos.intelligence ->
# aitos.intelligence.capital_runtime -> aitos.trading.lifecycle, closing the
# loop before TradeLifecycle existed yet). It only "worked" for entry points
# that happened to import aitos.app or aitos.intelligence first; importing
# aitos.trading, aitos.kernel, or aitos.trading.lifecycle directly, as the
# first import in a process, raised ImportError.
