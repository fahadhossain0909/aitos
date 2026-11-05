"""Startup recovery for durable capital-rebalance switch intents.

The worker runs only after TradeLifecycle has hydrated persisted trades and
venue reconciliation has completed. It never invents a replacement payload:
the complete Opportunity snapshot is persisted with the intent. Ambiguous
exchange outcomes remain fail-closed for operator reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aitos.models.trade import Opportunity, TradeSide


@dataclass(frozen=True)
class RecoveryReport:
    scanned: int
    resumed: int
    pending: int
    manual_reconciliation: int
    errors: tuple[str, ...] = ()


class PositionSwitchRecoveryWorker:
    """Resume incomplete intents after trade and exchange state hydration."""

    POST_CLOSE_STATES = {
        "close_submitting",
        "close_confirmed",
        "reservation_released",
        "open_ready",
        "open_submitting",
    }

    def __init__(self, coordinator: Any) -> None:
        if coordinator is None:
            raise ValueError("position switch coordinator is required")
        self.coordinator = coordinator

    @staticmethod
    def _candidate(payload: dict[str, Any]) -> Opportunity:
        if not payload:
            raise ValueError("pending switch has no durable candidate payload")
        values = dict(payload)
        values["side"] = TradeSide(str(values["side"]).upper())
        required = {
            "symbol",
            "side",
            "entry_price",
            "stop_loss_price",
            "take_profit_levels",
            "confidence",
            "strategy_id",
            "rationale",
            "opportunity_id",
            "detected_at",
        }
        missing = required.difference(values)
        if missing:
            raise ValueError(f"candidate payload missing fields: {sorted(missing)}")
        return Opportunity(**values)

    def _incumbent(self, trade_id: str) -> Any | None:
        lifecycle = self.coordinator.lifecycle
        return next(
            (
                trade
                for trade in [
                    *lifecycle.get_open_trades(),
                    *lifecycle.get_closed_trades(),
                ]
                if trade.trade_id == trade_id
            ),
            None,
        )

    async def recover_pending(self) -> RecoveryReport:
        intents = self.coordinator.journal.list_incomplete()
        resumed = pending = manual = 0
        errors: list[str] = []
        for intent in intents:
            try:
                candidate = self._candidate(intent.candidate_payload)
                incumbent = self._incumbent(intent.incumbent_trade_id)
                if incumbent is None:
                    raise RuntimeError("incumbent trade is not hydrated")
                if intent.state in self.POST_CLOSE_STATES:
                    # switch() uses the journal state to resume after close and
                    # will reconcile open_submitting rather than resubmit blindly.
                    decision = type("RecoveryDecision", (), {"should_switch": True})()
                else:
                    evaluator = self.coordinator.evaluate_pair
                    if evaluator is None:
                        raise RuntimeError(
                            "fresh pair evaluator unavailable at startup"
                        )
                    value = evaluator(incumbent, candidate)
                    decision = await value if hasattr(value, "__await__") else value
                    if not getattr(decision, "should_switch", False):
                        pending += 1
                        continue
                result = await self.coordinator.switch(
                    incumbent=incumbent, candidate=candidate, decision=decision
                )
                if result is not None:
                    resumed += 1
                else:
                    pending += 1
            except Exception as exc:
                state = self.coordinator.journal.get(intent.switch_id)
                if state and state.state == "manual_reconciliation_required":
                    manual += 1
                else:
                    pending += 1
                errors.append(f"{intent.switch_id}: {type(exc).__name__}: {exc}")
        return RecoveryReport(
            scanned=len(intents),
            resumed=resumed,
            pending=pending,
            manual_reconciliation=manual,
            errors=tuple(errors),
        )
