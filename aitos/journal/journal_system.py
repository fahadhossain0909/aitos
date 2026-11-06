"""JournalSystem — trade journal plus autonomous decision/outcome attribution."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.core.exceptions import ModuleNotInitializedError
from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.journal import reviews
from aitos.journal.decision_repository import DecisionJournalRepository
from aitos.journal.models import (
    DailyReview,
    JournalEntry,
    JournalEntryType,
    MonthlyReview,
    WeeklyReview,
)
from aitos.journal.repository import JournalRepository
from aitos.logging_setup import get_logger
from aitos.models.trade import Trade
from aitos.risk.risk_engine import RiskEngine
from aitos.xai.explanation import TradeExplanation, build_trade_explanation

logger = get_logger("aitos.journal.system")

TOPIC_DAILY_REVIEW = "journal.daily_review"
TOPIC_WEEKLY_REVIEW = "journal.weekly_review"
TOPIC_MONTHLY_REVIEW = "journal.monthly_review"
TOPIC_MISTAKE_RECORDED = "journal.mistake_recorded"
TOPIC_DECISION_RECORDED = "journal.decision_recorded"
TOPIC_OUTCOME_ATTRIBUTED = "journal.outcome_attributed"
TOPIC_MANAGEMENT_DECISION_RECORDED = "journal.management_decision_recorded"


class JournalSystem(AITOSModule):
    def __init__(
        self,
        event_bus: EventBus,
        repository: JournalRepository | None = None,
        risk_engine: RiskEngine | None = None,
        decision_repository: DecisionJournalRepository | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._repository = repository
        self._risk_engine = risk_engine
        self._decision_repository = decision_repository
        self._initialized = False
        self._subscriptions: list[Subscription] = []
        self._explanations: dict[str, TradeExplanation] = {}
        self._entries: list[JournalEntry] = []
        self._decision_snapshots: dict[str, dict[str, Any]] = {}
        self._decision_trade_ids: dict[str, str] = {}
        self._management_decisions_by_trade: dict[str, list[str]] = {}
        self._pending_decisions: dict[tuple[str, str], list[str]] = {}
        self._last_event_time: str | None = None

    @property
    def module_id(self) -> str:
        return "journal-system"

    @property
    def version(self) -> str:
        return "1.1.1"

    async def initialize(self, config: dict[str, Any]) -> None:
        if self._initialized:
            return
        self._subscriptions.extend(
            [
                await self._event_bus.subscribe(
                    "trade.position_opened", self._on_position_opened, group="journal"
                ),
                await self._event_bus.subscribe(
                    "trade.position_closed", self._on_position_closed, group="journal"
                ),
                await self._event_bus.subscribe(
                    "trade.rejected", self._on_rejected, group="journal"
                ),
                await self._event_bus.subscribe(
                    "decision.exit", self._on_exit_decision, group="journal"
                ),
            ]
        )
        if self._decision_repository is not None:
            self._subscriptions.extend(
                [
                    await self._event_bus.subscribe(
                        "decision.snapshot",
                        self._on_decision_snapshot,
                        group="decision-journal",
                    ),
                    await self._event_bus.subscribe(
                        "decision.opportunity",
                        self._on_decision_opportunity,
                        group="decision-journal",
                    ),
                ]
            )
        self._initialized = True
        logger.info("JournalSystem initialized")

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            module_id=self.module_id,
            status=(
                ModuleStatus.HEALTHY if self._initialized else ModuleStatus.UNHEALTHY
            ),
            latency_ms=0.0,
            last_event_time=self._last_event_time,
            details={
                "entries_recorded": len(self._entries),
                "explanations_cached": len(self._explanations),
                "decisions_tracked": len(self._decision_snapshots),
                "decision_outcomes_linked": len(self._decision_trade_ids),
            },
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        for subscription in self._subscriptions:
            subscription.cancel()
        self._subscriptions.clear()

    async def emit_events(self) -> AsyncIterator[Event]:
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Event) -> EventResponse | None:
        return None

    def get_explanation(self, trade_id: str) -> TradeExplanation | None:
        return self._explanations.get(trade_id)

    def get_entries(self) -> list[JournalEntry]:
        return list(self._entries)

    def get_decision_snapshot(self, decision_id: str) -> dict[str, Any] | None:
        snapshot = self._decision_snapshots.get(decision_id)
        return dict(snapshot) if snapshot is not None else None

    def get_decision_trade_id(self, decision_id: str) -> str | None:
        return self._decision_trade_ids.get(decision_id)

    async def record_mistake(
        self,
        trade_id: str,
        mistake: str,
        lesson: str | None = None,
        improvement: str | None = None,
    ) -> JournalEntry:
        self._require_initialized()
        entry = JournalEntry(
            trade_id=trade_id,
            entry_type=JournalEntryType.MISTAKE,
            market_context={},
            mistakes=[mistake],
            lessons=[lesson] if lesson else [],
            improvements=[improvement] if improvement else [],
        )
        await self._persist(entry)
        await self._event_bus.publish(
            Event(
                topic=TOPIC_MISTAKE_RECORDED,
                payload=entry.to_dict(),
                source_module=self.module_id,
            )
        )
        return entry

    async def generate_daily_review(
        self, trades: list[Trade], date: str
    ) -> DailyReview:
        self._require_initialized()
        review = reviews.daily_review(trades, date)
        await self._persist(
            JournalEntry(
                trade_id=None,
                entry_type=JournalEntryType.DAILY,
                market_context=review.to_dict(),
            )
        )
        await self._event_bus.publish(
            Event(
                topic=TOPIC_DAILY_REVIEW,
                payload=review.to_dict(),
                source_module=self.module_id,
            )
        )
        return review

    async def generate_weekly_review(
        self, trades: list[Trade], week_start: str
    ) -> WeeklyReview:
        self._require_initialized()
        review = reviews.weekly_review(trades, week_start)
        await self._persist(
            JournalEntry(
                trade_id=None,
                entry_type=JournalEntryType.WEEKLY,
                market_context=review.to_dict(),
            )
        )
        await self._event_bus.publish(
            Event(
                topic=TOPIC_WEEKLY_REVIEW,
                payload=review.to_dict(),
                source_module=self.module_id,
            )
        )
        return review

    async def generate_monthly_review(
        self, trades: list[Trade], month: str, starting_equity: float = 10_000.0
    ) -> MonthlyReview:
        self._require_initialized()
        review = reviews.monthly_review(trades, month, starting_equity)
        await self._persist(
            JournalEntry(
                trade_id=None,
                entry_type=JournalEntryType.MONTHLY,
                market_context=review.to_dict(),
            )
        )
        await self._event_bus.publish(
            Event(
                topic=TOPIC_MONTHLY_REVIEW,
                payload=review.to_dict(),
                source_module=self.module_id,
            )
        )
        return review

    async def _on_decision_snapshot(self, event: Event) -> EventResponse | None:
        if self._decision_repository is None:
            return None
        payload = dict(event.payload)
        decision_id = str(payload.get("decision_id") or event.event_id)
        payload["decision_id"] = decision_id
        self._decision_snapshots[decision_id] = payload
        key = (str(payload.get("symbol", "")), str(payload.get("side", "")))
        self._pending_decisions.setdefault(key, []).append(decision_id)
        await self._decision_repository.save_decision(decision_id, payload)
        self._last_event_time = event.created_at
        await self._event_bus.publish(
            Event(
                topic=TOPIC_DECISION_RECORDED,
                payload=payload,
                source_module=self.module_id,
            )
        )
        return None

    async def _on_decision_opportunity(self, event: Event) -> EventResponse | None:
        if self._decision_repository is None:
            return None
        payload = dict(event.payload)
        key = (str(payload.get("symbol", "")), str(payload.get("side", "")))
        if self._pending_decisions.get(key):
            return None
        payload["decision_id"] = str(payload.get("decision_id") or event.event_id)
        return await self._on_decision_snapshot(
            Event(
                topic="decision.snapshot",
                payload=payload,
                source_module=event.source_module,
                created_at=event.created_at,
            )
        )

    async def _on_exit_decision(self, event: Event) -> EventResponse | None:
        """Persist every HOLD/MANAGE/EXIT decision against its open trade."""
        payload = dict(event.payload)
        trade_id = str(payload.get("trade_id") or "")
        if not trade_id:
            logger.warning("exit decision missing trade_id; attribution skipped")
            return None
        decision_id = f"{trade_id}:management:{event.event_id}"
        snapshot = {
            **payload,
            "decision_id": decision_id,
            "trade_id": trade_id,
            "decision_type": "exit_management",
            "action": str(payload.get("action") or "unknown").upper(),
            "event_time": event.created_at,
        }
        self._management_decisions_by_trade.setdefault(trade_id, []).append(decision_id)
        if self._decision_repository is not None:
            save_management = getattr(
                self._decision_repository, "save_management_decision", None
            )
            if callable(save_management):
                await save_management(decision_id, trade_id, snapshot)
        await self._event_bus.publish(
            Event(
                topic=TOPIC_MANAGEMENT_DECISION_RECORDED,
                payload=snapshot,
                source_module=self.module_id,
            )
        )
        self._last_event_time = event.created_at
        return None

    async def _on_position_opened(self, event: Event) -> EventResponse | None:
        trade_dict = dict(event.payload)
        raw_consensus = trade_dict.get("agent_consensus") or {}
        numeric_consensus = {
            key: value
            for key, value in raw_consensus.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        explanation_payload = dict(trade_dict)
        explanation_payload["agent_consensus"] = numeric_consensus
        risk_assessment = (
            self._risk_engine.last_assessment if self._risk_engine else None
        )
        explanation = build_trade_explanation(
            explanation_payload, risk_assessment=risk_assessment
        )
        trade_id = trade_dict.get("trade_id", "")
        self._explanations[trade_id] = explanation
        entry = JournalEntry(
            trade_id=trade_id,
            entry_type=JournalEntryType.PRE_TRADE,
            market_context={
                "symbol": trade_dict.get("symbol"),
                "entry_price": trade_dict.get("entry_price"),
                "explanation": explanation.to_dict(),
            },
            confidence_score=explanation.confidence_score,
            order_flow_observations={
                "order_flow_bias": raw_consensus.get("order_flow_bias")
            },
            liquidity_observations={
                "liquidity_quality": raw_consensus.get("liquidity_quality")
            },
            amt_observations={
                "auction_context": raw_consensus.get("auction_context"),
                "market_regime": raw_consensus.get("market_regime"),
            },
            lead_lag_observations={"lead_lag": raw_consensus.get("lead_lag")},
        )
        await self._persist(entry)
        if self._repository is not None:
            await self._repository.save_trade_snapshot(trade_dict)
        if self._decision_repository is not None:
            decision_id = raw_consensus.get("decision_id")
            if not decision_id:
                key = (
                    str(trade_dict.get("symbol", "")),
                    str(trade_dict.get("side", "")),
                )
                pending = self._pending_decisions.get(key, [])
                decision_id = pending.pop(0) if pending else None
            if decision_id:
                self._decision_trade_ids[str(decision_id)] = trade_id
                await self._decision_repository.link_trade(str(decision_id), trade_dict)
        self._last_event_time = entry.created_at
        return None

    async def _on_position_closed(self, event: Event) -> EventResponse | None:
        trade_dict = dict(event.payload)
        trade_id = trade_dict.get("trade_id")
        entry = JournalEntry(
            trade_id=trade_id,
            entry_type=JournalEntryType.POST_TRADE,
            market_context={
                "symbol": trade_dict.get("symbol"),
                "exit_price": trade_dict.get("exit_price"),
                "exit_reason": trade_dict.get("exit_reason"),
                "pnl": trade_dict.get("pnl"),
                "pnl_percent": trade_dict.get("pnl_percent"),
            },
        )
        await self._persist(entry)
        if self._repository is not None:
            await self._repository.save_trade_snapshot(trade_dict)
        trade_records = []
        if self._decision_repository is not None and trade_id:
            get_trade_records = getattr(
                self._decision_repository, "get_trade_records", None
            )
            if callable(get_trade_records):
                try:
                    trade_records = await get_trade_records(str(trade_id))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "trade decision recovery failed during outcome attribution",
                        extra={
                            "aitos_extra": {
                                "trade_id": str(trade_id),
                                "error": str(exc),
                            }
                        },
                    )
        decision_id = next(
            (did for did, tid in self._decision_trade_ids.items() if tid == trade_id),
            None,
        )
        if not decision_id:
            trade_link = next(
                (
                    record
                    for record in trade_records
                    if record.get("record_type") == "TRADE_LINK"
                ),
                None,
            )
            decision_id = trade_link.get("decision_id") if trade_link else None
        if not decision_id:
            decision_id = (trade_dict.get("agent_consensus") or {}).get("decision_id")
        decision_snapshot = (
            dict(self._decision_snapshots.get(str(decision_id), {}))
            if decision_id
            else {}
        )
        if (
            decision_id
            and not decision_snapshot
            and self._decision_repository is not None
        ):
            get_records = getattr(self._decision_repository, "get_records", None)
            if callable(get_records):
                try:
                    records = await get_records(str(decision_id))
                    for record in records:
                        if record.get("record_type") != "DECISION":
                            continue
                        raw_snapshot = record.get("payload")
                        snapshot = (
                            json.loads(raw_snapshot)
                            if isinstance(raw_snapshot, str)
                            else raw_snapshot
                        )
                        if isinstance(snapshot, dict):
                            decision_snapshot = snapshot
                            self._decision_snapshots[str(decision_id)] = snapshot
                            break
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "decision snapshot recovery failed during outcome attribution",
                        extra={
                            "aitos_extra": {
                                "decision_id": str(decision_id),
                                "error": str(exc),
                            }
                        },
                    )
        memory_management_ids = list(
            self._management_decisions_by_trade.pop(str(trade_id), [])
        )
        management_ids = [
            str(record.get("decision_id"))
            for record in trade_records
            if record.get("record_type") == "MANAGEMENT_DECISION"
            and record.get("decision_id")
        ]
        if not management_ids:
            management_ids = memory_management_ids
        if self._decision_repository is not None:
            if decision_id:
                await self._decision_repository.attribute_outcome(
                    str(decision_id), trade_dict
                )
            for management_id in management_ids:
                await self._decision_repository.attribute_outcome(
                    management_id, trade_dict
                )
        # Every completed trade is a learning outcome, even when the upstream
        # scanner did not attach a decision-journal ID. The recorder links by
        # trade_id; decision_id remains optional metadata rather than a gate.
        await self._event_bus.publish(
            Event(
                topic=TOPIC_OUTCOME_ATTRIBUTED,
                payload={
                    "decision_id": str(decision_id or ""),
                    "trade_id": trade_id,
                    "pnl": trade_dict.get("pnl"),
                    "pnl_percent": trade_dict.get("pnl_percent"),
                    "risk_amount_usd": trade_dict.get("risk_amount_usd"),
                    "strategy_id": trade_dict.get("strategy_id"),
                    "symbol": trade_dict.get("symbol"),
                    "side": trade_dict.get("side"),
                    "entry_price": trade_dict.get("entry_price"),
                    "exit_price": trade_dict.get("exit_price"),
                    "exit_reason": trade_dict.get("exit_reason"),
                    "regime": trade_dict.get("regime"),
                    "mae_price": trade_dict.get("mae_price"),
                    "mfe_price": trade_dict.get("mfe_price"),
                    "mae_r": trade_dict.get("mae_r"),
                    "mfe_r": trade_dict.get("mfe_r"),
                    "agent_consensus": dict(trade_dict.get("agent_consensus") or {}),
                    "decision_snapshot": decision_snapshot,
                    "root_cause_tags": list(trade_dict.get("root_cause_tags") or []),
                    "total_cost_r": trade_dict.get("total_cost_r"),
                    "diagnostics": dict(trade_dict.get("diagnostics") or {}),
                    "management_decision_ids": management_ids,
                    "state": "closed",
                },
                source_module=self.module_id,
            )
        )
        self._last_event_time = entry.created_at
        return None

    async def _on_rejected(self, event: Event) -> EventResponse | None:
        trade_dict = dict(event.payload)
        decision_id = None
        entry = JournalEntry(
            trade_id=trade_dict.get("trade_id"),
            entry_type=JournalEntryType.PRE_TRADE,
            market_context={
                "rejected": True,
                "reason": trade_dict.get("rejection_reason"),
                "symbol": trade_dict.get("symbol"),
            },
        )
        await self._persist(entry)
        if self._decision_repository is not None:
            decision_id = (trade_dict.get("agent_consensus") or {}).get("decision_id")
            if not decision_id:
                key = (
                    str(trade_dict.get("symbol", "")),
                    str(trade_dict.get("side", "")),
                )
                pending = self._pending_decisions.get(key, [])
                decision_id = pending.pop(0) if pending else None
            if decision_id:
                self._decision_trade_ids[str(decision_id)] = trade_dict.get(
                    "trade_id", ""
                )
                await self._decision_repository.attribute_outcome(
                    str(decision_id), trade_dict
                )
        reason = str(trade_dict.get("rejection_reason") or "")
        execution_failure = (
            "order submission failed" in reason.lower()
            or "execution" in reason.lower()
            or "fill" in reason.lower()
        )
        decision_id = str(decision_id or "")
        snapshot = (
            dict(self._decision_snapshots.get(decision_id, {})) if decision_id else {}
        )
        await self._event_bus.publish(
            Event(
                topic=TOPIC_OUTCOME_ATTRIBUTED,
                payload={
                    "decision_id": decision_id,
                    "trade_id": trade_dict.get("trade_id"),
                    "symbol": trade_dict.get("symbol"),
                    "side": trade_dict.get("side"),
                    "strategy_id": trade_dict.get("strategy_id"),
                    "state": "rejected",
                    "outcome": "execution_failure" if execution_failure else "rejected",
                    "execution_failure": execution_failure,
                    "rejection_reason": reason,
                    "decision_snapshot": snapshot,
                },
                source_module=self.module_id,
            )
        )
        self._last_event_time = entry.created_at
        return None

    async def _persist(self, entry: JournalEntry) -> None:
        self._entries.append(entry)
        if self._repository is not None:
            await self._repository.save_journal_entry(entry)

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError(
                "JournalSystem.initialize() must be called first"
            )
