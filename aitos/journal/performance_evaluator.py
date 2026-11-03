"""Decision performance evaluation over the durable decision journal.

This module is intentionally read-only: it measures historical decision quality
without changing production policy. It is the safety boundary between outcome
attribution and the future adaptive-policy engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.journal.decision_repository import DecisionJournalRepository


@dataclass(frozen=True)
class PerformanceSlice:
    """Aggregate performance for one categorical slice."""

    key: str
    value: str
    trades: int
    wins: int
    losses: int
    total_pnl: float
    average_pnl: float
    average_r_multiple: float
    win_rate: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": self.total_pnl,
            "average_pnl": self.average_pnl,
            "average_r_multiple": self.average_r_multiple,
            "win_rate": self.win_rate,
        }


@dataclass(frozen=True)
class PerformanceReport:
    decision_count: int
    outcome_count: int
    linked_trade_count: int
    total_pnl: float
    average_pnl: float
    average_r_multiple: float
    win_rate: float
    slices: list[PerformanceSlice] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_count": self.decision_count,
            "outcome_count": self.outcome_count,
            "linked_trade_count": self.linked_trade_count,
            "total_pnl": self.total_pnl,
            "average_pnl": self.average_pnl,
            "average_r_multiple": self.average_r_multiple,
            "win_rate": self.win_rate,
            "slices": [item.to_dict() for item in self.slices],
        }


class DecisionPerformanceEvaluator(AITOSModule):
    """Read-only evaluator for decision/outcome history.

    It deliberately does not mutate weights, policies, or RL state. That keeps
    evaluation deterministic and makes the next adaptive-policy phase safer.
    """

    def __init__(self, decision_repository: DecisionJournalRepository) -> None:
        self._repository = decision_repository
        self._initialized = False
        self._last_report: PerformanceReport | None = None

    @property
    def module_id(self) -> str:
        return "decision-performance-evaluator"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: dict[str, Any]) -> None:
        self._initialized = True

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        self._initialized = False

    async def emit_events(self):
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Event) -> EventResponse | None:
        return None

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            module_id=self.module_id,
            status=(
                ModuleStatus.HEALTHY if self._initialized else ModuleStatus.UNHEALTHY
            ),
            latency_ms=0.0,
            last_event_time=None,
            details={"has_report": self._last_report is not None},
        )

    async def evaluate_decision(self, decision_id: str) -> PerformanceReport:
        """Evaluate one decision and all outcome records attached to it."""
        records = await self._repository.get_records(decision_id)
        return self._build_report(records)

    async def evaluate_decisions(self, decision_ids: list[str]) -> PerformanceReport:
        """Evaluate a supplied set of decisions without querying policy state."""
        records: list[dict[str, Any]] = []
        for decision_id in decision_ids:
            records.extend(await self._repository.get_records(decision_id))
        return self._build_report(records)

    def last_report(self) -> PerformanceReport | None:
        return self._last_report

    def _build_report(self, records: list[dict[str, Any]]) -> PerformanceReport:
        decisions = [r for r in records if r.get("record_type") == "DECISION"]
        links = [r for r in records if r.get("record_type") == "TRADE_LINK"]
        outcomes = [r for r in records if r.get("record_type") == "OUTCOME"]

        pnl_values = [float(r["pnl"]) for r in outcomes if r.get("pnl") is not None]
        r_values = [
            float(r["r_multiple"]) for r in outcomes if r.get("r_multiple") is not None
        ]
        wins = sum(1 for value in pnl_values if value > 0)
        losses = sum(1 for value in pnl_values if value < 0)
        report = PerformanceReport(
            decision_count=len(decisions),
            outcome_count=len(outcomes),
            linked_trade_count=len(links),
            total_pnl=sum(pnl_values),
            average_pnl=sum(pnl_values) / len(pnl_values) if pnl_values else 0.0,
            average_r_multiple=sum(r_values) / len(r_values) if r_values else 0.0,
            win_rate=wins / len(pnl_values) if pnl_values else 0.0,
            slices=self._build_slices(outcomes),
        )
        self._last_report = report
        return report

    @staticmethod
    def _build_slices(outcomes: list[dict[str, Any]]) -> list[PerformanceSlice]:
        buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for record in outcomes:
            for key in ("symbol", "side", "strategy_id", "regime", "exit_reason"):
                value = str(record.get(key) or "unknown")
                buckets.setdefault((key, value), []).append(record)

        result: list[PerformanceSlice] = []
        for (key, value), records in sorted(buckets.items()):
            pnl = [float(r["pnl"]) for r in records if r.get("pnl") is not None]
            rs = [
                float(r["r_multiple"])
                for r in records
                if r.get("r_multiple") is not None
            ]
            wins = sum(1 for p in pnl if p > 0)
            losses = sum(1 for p in pnl if p < 0)
            result.append(
                PerformanceSlice(
                    key=key,
                    value=value,
                    trades=len(pnl),
                    wins=wins,
                    losses=losses,
                    total_pnl=sum(pnl),
                    average_pnl=sum(pnl) / len(pnl) if pnl else 0.0,
                    average_r_multiple=sum(rs) / len(rs) if rs else 0.0,
                    win_rate=wins / len(pnl) if pnl else 0.0,
                )
            )
        return result
