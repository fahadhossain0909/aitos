"""Event-driven persistence of paper/live decisions and outcomes."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from math import isfinite
from typing import Any

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.data.repository import MarketDataRepository
from aitos.eventbus.redis_bus import EventBus, Subscription

from .experience import ExperienceRecord
from .regime_feedback import outcome_feedback


def _event_time(event: Event) -> datetime:
    value = datetime.fromisoformat(event.created_at.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class LearningExperienceRecorder(AITOSModule):
    """Turn decision/outcome events into durable learning experiences."""

    def __init__(
        self,
        event_bus: EventBus,
        repository: MarketDataRepository | None,
        source: str,
    ) -> None:
        if source not in {"paper", "live"}:
            raise ValueError("source must be paper or live")
        self._event_bus = event_bus
        self._repository = repository
        self._source = source
        self._subscriptions: list[Subscription] = []
        self._initialized = False
        self._records_written = 0
        self._seen_experience_ids: set[str] = set()
        self._decision_events_received = 0
        self._outcome_events_received = 0
        self._last_event_time: str | None = None

    @property
    def module_id(self) -> str:
        return f"learning-experience-recorder-{self._source}"

    @property
    def version(self) -> str:
        return "1.1.0"

    async def initialize(self, config: dict[str, Any]) -> None:
        if self._initialized:
            return
        if self._repository is not None:
            await self._repository.ensure_learning_experience_schema()
        self._subscriptions.clear()
        try:
            self._subscriptions.append(
                await self._event_bus.subscribe(
                    "journal.decision_recorded",
                    self._on_decision,
                    group=f"learning-{self._source}",
                )
            )
            self._subscriptions.append(
                await self._event_bus.subscribe(
                    "journal.outcome_attributed",
                    self._on_outcome,
                    group=f"learning-{self._source}",
                )
            )
            self._subscriptions.append(
                await self._event_bus.subscribe(
                    "journal.management_decision_recorded",
                    self._on_management_decision,
                    group=f"learning-{self._source}",
                )
            )
        except Exception:
            for subscription in self._subscriptions:
                subscription.cancel()
            self._subscriptions.clear()
            raise
        self._initialized = True

    async def health_check(self) -> HealthStatus:
        status = (
            ModuleStatus.UNHEALTHY
            if not self._initialized
            else (
                ModuleStatus.HEALTHY
                if self._repository is not None
                else ModuleStatus.DEGRADED
            )
        )
        return HealthStatus(
            module_id=self.module_id,
            status=status,
            latency_ms=0.0,
            last_event_time=self._last_event_time,
            details={
                "records_written": self._records_written,
                "decision_events_received": self._decision_events_received,
                "outcome_events_received": self._outcome_events_received,
                "source": self._source,
            },
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        for sub in self._subscriptions:
            sub.cancel()
        self._subscriptions.clear()
        self._initialized = False

    async def emit_events(self) -> AsyncIterator[Event]:
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Event) -> EventResponse | None:
        return None

    def _stable_experience_id(self, event_type: str, identity: str) -> str:
        """Derive stable IDs so redelivery/restart reuses the same insert token."""
        value = f"aitos:{self._source}:{event_type}:{identity}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, value))

    async def _write(self, record: ExperienceRecord) -> None:
        if record.experience_id in self._seen_experience_ids:
            return
        if self._repository is not None:
            await self._repository.save_learning_experience(record)
            self._records_written += 1
        self._seen_experience_ids.add(record.experience_id)

    async def _on_decision(self, event: Event) -> EventResponse | None:
        self._decision_events_received += 1
        p = dict(event.payload)
        decision_id = str(p.get("decision_id") or event.event_id)
        record = ExperienceRecord(
            timestamp=_event_time(event),
            source=self._source,
            symbol=str(p.get("symbol", "unknown")),
            decision=str(p.get("side", p.get("direction", "unknown"))),
            experience_id=self._stable_experience_id("decision", decision_id),
            confidence=float(p.get("confidence", 0.0) or 0.0),
            quantity=float(p.get("quantity", p.get("position_size", 0.0)) or 0.0),
            price=float(p["entry_price"]) if p.get("entry_price") is not None else None,
            features=dict(p.get("agent_consensus") or {}),
            market_state={
                "regime": p.get("regime"),
                "kernel_direction": p.get("kernel_direction"),
                "kernel_confidence": p.get("kernel_confidence"),
            },
            risk_state=dict(p.get("risk_state") or {}),
            strategy_version=str(p.get("strategy_id", "unknown")),
            model_version=str(p.get("model_version", "unknown")),
            metadata={
                "decision_id": decision_id,
                "source_event_id": str(event.event_id),
                "event_type": "decision",
                "decision_diagnostics": {
                    "candidate_score": p.get("candidate_score"),
                    "component_scores": dict(p.get("candidate_component_scores") or {}),
                    "component_availability": dict(
                        p.get("candidate_component_availability") or {}
                    ),
                    "weights": dict(p.get("candidate_weights") or {}),
                    "threshold": p.get("candidate_threshold"),
                    "rationale": list(
                        p.get("candidate_rationale") or p.get("rationale") or []
                    ),
                },
            },
        )
        await self._write(record)
        self._last_event_time = event.created_at
        return None

    async def _on_management_decision(self, event: Event) -> EventResponse | None:
        """Retain HOLD/MANAGE/EXIT observations without treating them as entries."""
        p = dict(event.payload)
        decision_id = str(p.get("decision_id") or event.event_id)
        record = ExperienceRecord(
            timestamp=_event_time(event),
            source=self._source,
            symbol=str(p.get("symbol") or "unknown"),
            experience_id=self._stable_experience_id("management", decision_id),
            decision=str(p.get("action") or "UNKNOWN").upper(),
            confidence=0.0,
            outcome=None,
            features=dict(p.get("features") or {}),
            market_state={"regime": p.get("regime")},
            metadata={
                "decision_id": decision_id,
                "source_event_id": str(event.event_id),
                "trade_id": str(p.get("trade_id") or ""),
                "event_type": "management_decision",
                "decision_type": "exit_management",
                "reason": p.get("reason"),
                "reasons": list(p.get("reasons") or []),
            },
        )
        await self._write(record)
        self._last_event_time = event.created_at
        return None

    async def _on_outcome(self, event: Event) -> EventResponse | None:
        self._outcome_events_received += 1
        p = dict(event.payload)
        state = str(p.get("state") or "").lower()
        if state in {"rejected", "execution_failure"}:
            identity = str(p.get("trade_id") or p.get("decision_id") or event.event_id)
            record = ExperienceRecord(
                timestamp=_event_time(event),
                source=self._source,
                symbol=str(p.get("symbol") or "unknown"),
                decision=str(p.get("side") or "UNKNOWN"),
                outcome=state,
                reward=0.0,
                experience_id=self._stable_experience_id("rejection", identity),
                metadata={
                    "decision_id": str(p.get("decision_id") or ""),
                    "source_event_id": str(event.event_id),
                    "trade_id": str(p.get("trade_id") or ""),
                    "event_type": "decision_outcome",
                    "execution_failure": bool(
                        p.get("execution_failure", state == "execution_failure")
                    ),
                    "rejection_reason": p.get("rejection_reason"),
                    "reward_unit": "not_applicable",
                },
            )
            await self._write(record)
            self._last_event_time = event.created_at
            return None
        try:
            pnl = float(p.get("pnl"))
        except (TypeError, ValueError):
            return None
        if not isfinite(pnl):
            return None

        try:
            risk_amount = float(p.get("risk_amount_usd"))
        except (TypeError, ValueError):
            risk_amount = 0.0
        has_risk = isfinite(risk_amount) and risk_amount > 0
        reward = pnl / risk_amount if has_risk else pnl
        reward_unit = "r_multiple" if has_risk else "unscaled_quote_pnl"

        metadata = {
            "decision_id": str(p.get("decision_id", "")),
            "source_event_id": str(event.event_id),
            "trade_id": str(p.get("trade_id", "")),
            "strategy_id": str(p.get("strategy_id") or "unknown"),
            "regime": p.get("regime"),
            "event_type": "outcome",
            "reward_unit": reward_unit,
            "raw_pnl_usd": pnl,
            "entry_price": p.get("entry_price"),
            "exit_price": p.get("exit_price"),
            "exit_reason": p.get("exit_reason"),
            "pnl_percent": p.get("pnl_percent"),
            "mae_price": p.get("mae_price"),
            "mfe_price": p.get("mfe_price"),
            "mae_r": p.get("mae_r"),
            "mfe_r": p.get("mfe_r"),
            "decision_snapshot": dict(p.get("decision_snapshot") or {}),
            "root_cause_tags": list(p.get("root_cause_tags") or []),
            "total_cost_r": p.get("total_cost_r"),
            "management_decision_ids": list(p.get("management_decision_ids") or []),
            "diagnostics": dict(p.get("diagnostics") or {}),
        }
        if has_risk:
            try:
                mfe_r = float(p.get("mfe_r"))
                feedback = outcome_feedback(
                    realized_r=reward,
                    mfe_r=mfe_r,
                    exit_reason=str(p.get("exit_reason") or ""),
                )
            except (TypeError, ValueError):
                metadata["exit_feedback_status"] = "unavailable"
            else:
                metadata.update(
                    {
                        "exit_feedback_status": "available",
                        "premature_exit": feedback.premature_exit,
                        "missed_opportunity_r": feedback.missed_opportunity_r,
                        "realized_r": feedback.realized_r,
                    }
                )
        else:
            metadata["exit_feedback_status"] = "unavailable_missing_risk"

        outcome_identity = str(
            p.get("trade_id") or p.get("decision_id") or event.event_id
        )
        record = ExperienceRecord(
            timestamp=_event_time(event),
            source=self._source,
            symbol=str(p.get("symbol", "unknown")),
            decision=str(p.get("side", "outcome")),
            outcome="closed",
            experience_id=self._stable_experience_id(
                "closed_outcome", outcome_identity
            ),
            reward=reward,
            price=(
                float(p["entry_price"]) if p.get("entry_price") is not None else None
            ),
            features=dict(p.get("agent_consensus") or {}),
            market_state={"regime": p.get("regime")},
            risk_state={"risk_amount_usd": risk_amount} if has_risk else {},
            strategy_version=str(p.get("strategy_id") or "unknown"),
            metadata=metadata,
        )
        await self._write(record)
        self._last_event_time = event.created_at
        return None
