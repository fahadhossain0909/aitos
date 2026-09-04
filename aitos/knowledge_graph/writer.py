"""Event-driven Neo4j knowledge graph for AITOS.

Neo4j is deliberately a semantic relationship layer, not a high-frequency
market-data store. Redis/EventBus is the live transport, ClickHouse is the
canonical durable analytical store, and this writer projects selected,
low-volume decision/intelligence/trade semantics into a graph.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Protocol

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.core.exceptions import ModuleNotInitializedError
from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.logging_setup import get_logger

logger = get_logger("aitos.knowledge_graph.writer")


class GraphSession(Protocol):
    async def run(self, query: str, **params: Any) -> Any: ...


class GraphDriver(Protocol):
    def session(self) -> Any: ...

    async def close(self) -> None: ...


CREATE_TRADE_QUERY = """
MERGE (s:Symbol {name: $symbol})
MERGE (strat:Strategy {id: $strategy_id})
MERGE (t:Trade {id: $trade_id})
SET t.side = $side, t.entry_price = $entry_price, t.regime = $regime,
    t.state = $state, t.opened_at = $entry_time
MERGE (t)-[:ON_SYMBOL]->(s)
MERGE (t)-[:USED_STRATEGY]->(strat)
"""

CLOSE_TRADE_QUERY = """
MATCH (t:Trade {id: $trade_id})
SET t.pnl = $pnl, t.pnl_percent = $pnl_percent, t.exit_price = $exit_price,
    t.exit_reason = $exit_reason, t.closed_at = $exit_time, t.state = $state
"""

LINK_MISTAKE_QUERY = """
MATCH (t:Trade {id: $trade_id})
MERGE (m:Mistake {id: $mistake_id})
SET m.text = $mistake_text, m.recorded_at = $created_at
MERGE (t)-[:HAD_MISTAKE]->(m)
"""

CORRELATION_QUERY = """
MERGE (a:Symbol {name: $symbol_a})
MERGE (b:Symbol {name: $symbol_b})
MERGE (a)-[r:CORRELATED_WITH]->(b)
SET r.coefficient = $coefficient, r.updated_at = $updated_at
"""

SEMANTIC_TOPICS = (
    "decision.*",
    "risk.*",
    "scanner.*",
    "statistics.*",
    "intelligence.*",
    "journey.*",
    "execution.*",
)

# Decision streams that are emitted by the current runtime. Registering these
# before the wildcard subscription is intentional: Redis wildcard resolution
# happens against streams known at subscription time, while the subscription
# itself remains a single wildcard subscription (no duplicate consumers).
_SEMANTIC_DECISION_STREAMS = (
    "decision.trade_candidate",
    "decision.generated",
    "decision.snapshot",
)

PROJECT_SEMANTIC_EVENT_QUERY = """
MERGE (e:KnowledgeEvent {id: $event_id})
SET e.topic = $topic,
    e.event_time = $event_time,
    e.source_module = $source_module,
    e.schema_version = $schema_version,
    e.payload_json = $payload_json

FOREACH (symbol IN CASE WHEN $symbol <> '' THEN [$symbol] ELSE [] END |
    MERGE (s:Symbol {name: symbol})
    MERGE (e)-[:ABOUT_SYMBOL]->(s))
FOREACH (strategy_id IN CASE WHEN $strategy_id <> '' THEN [$strategy_id] ELSE [] END |
    MERGE (st:Strategy {id: strategy_id})
    MERGE (e)-[:INVOLVES_STRATEGY]->(st))
FOREACH (model_id IN CASE WHEN $model_id <> '' THEN [$model_id] ELSE [] END |
    MERGE (m:Model {id: model_id})
    MERGE (e)-[:PRODUCED_BY_MODEL]->(m))
FOREACH (policy_id IN CASE WHEN $policy_id <> '' THEN [$policy_id] ELSE [] END |
    MERGE (p:Policy {id: policy_id})
    MERGE (e)-[:GOVERNED_BY_POLICY]->(p))
FOREACH (trade_id IN CASE WHEN $trade_id <> '' THEN [$trade_id] ELSE [] END |
    MERGE (t:Trade {id: trade_id})
    MERGE (e)-[:RELATES_TO_TRADE]->(t))
FOREACH (decision_id IN CASE WHEN $decision_id <> '' THEN [$decision_id] ELSE [] END |
    MERGE (d:Decision {id: decision_id})
    MERGE (e)-[:RELATES_TO_DECISION]->(d))
FOREACH (regime IN CASE WHEN $regime <> '' THEN [$regime] ELSE [] END |
    MERGE (r:MarketRegime {name: regime})
    MERGE (e)-[:OCCURRED_IN_REGIME]->(r))

FOREACH (risk_id IN CASE WHEN $risk_id <> '' THEN [$risk_id] ELSE [] END |
    MERGE (r:RiskDecision {id: risk_id})
    SET r.state = $risk_state, r.action = $risk_action,
        r.score = $risk_score
    MERGE (e)-[:HAS_RISK_DECISION]->(r))
FOREACH (execution_id IN CASE WHEN $execution_id <> '' THEN [$execution_id] ELSE [] END |
    MERGE (x:Execution {id: execution_id})
    SET x.side = $execution_side, x.status = $execution_status,
        x.price = $execution_price, x.quantity = $execution_quantity
    MERGE (e)-[:HAS_EXECUTION]->(x))
FOREACH (journey_id IN CASE WHEN $journey_id <> '' THEN [$journey_id] ELSE [] END |
    MERGE (j:TradeJourney {id: journey_id})
    SET j.state = $journey_state
    MERGE (e)-[:HAS_JOURNEY]->(j))
FOREACH (forecast_id IN CASE WHEN $forecast_id <> '' THEN [$forecast_id] ELSE [] END |
    MERGE (f:Forecast {id: forecast_id})
    SET f.probability = $forecast_probability, f.target = $forecast_target,
        f.horizon = $forecast_horizon
    MERGE (e)-[:REFERENCES_FORECAST]->(f))
FOREACH (outcome_id IN CASE WHEN $outcome_id <> '' THEN [$outcome_id] ELSE [] END |
    MERGE (o:Outcome {id: outcome_id})
    SET o.label = $outcome_label, o.pnl = $outcome_pnl
    MERGE (e)-[:REFERENCES_OUTCOME]->(o))
FOREACH (run_id IN CASE WHEN $run_id <> '' THEN [$run_id] ELSE [] END |
    MERGE (mr:ModelRun {id: run_id})
    SET mr.dataset_id = $dataset_id, mr.feature_set_version = $feature_set_version,
        mr.artifact_id = $artifact_id, mr.status = $run_status
    MERGE (e)-[:PART_OF_MODEL_RUN]->(mr))
FOREACH (calibration_id IN CASE WHEN $calibration_id <> '' THEN [$calibration_id] ELSE [] END |
    MERGE (c:CalibrationRun {id: calibration_id})
    SET c.method = $calibration_method, c.sample_count = $sample_count,
        c.brier = $brier, c.log_loss = $log_loss, c.ece = $ece
    MERGE (e)-[:PART_OF_CALIBRATION]->(c))

WITH e
UNWIND $evidence AS evidence
MERGE (ev:Evidence {id: evidence.id})
SET ev.kind = evidence.kind,
    ev.name = evidence.name,
    ev.value = evidence.value,
    ev.weight = evidence.weight
MERGE (e)-[:SUPPORTED_BY]->(ev)
"""


def _first(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _number(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _evidence(payload: dict[str, Any], event_id: str) -> list[dict[str, Any]]:
    raw = payload.get("evidence", payload.get("features", []))
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw[:32]):
        if isinstance(item, dict):
            name = _first(item, "name", "feature", "key", "type")
            if not name:
                continue
            result.append(
                {
                    "id": _first(item, "id", "evidence_id") or f"{event_id}:e:{index}",
                    "kind": _first(item, "kind", "type") or "feature",
                    "name": name,
                    "value": str(item.get("value", "")),
                    "weight": _number(item, "weight", "importance"),
                }
            )
        elif isinstance(item, str) and item.strip():
            result.append(
                {
                    "id": f"{event_id}:e:{index}",
                    "kind": "feature",
                    "name": item.strip(),
                    "value": "",
                    "weight": None,
                }
            )
    return result


class KnowledgeGraphWriter(AITOSModule):
    """Projects selected AITOS semantics into Neo4j without blocking live flow."""

    def __init__(self, event_bus: EventBus, driver: GraphDriver) -> None:
        self._event_bus = event_bus
        self._driver = driver
        self._initialized = False
        self._subscriptions: list[Subscription] = []
        self._writes_applied = 0
        self._errors = 0
        self._last_event_time: str | None = None

    @property
    def module_id(self) -> str:
        return "knowledge-graph-writer"

    @property
    def version(self) -> str:
        return "2.1.0"

    async def initialize(self, config: dict[str, Any]) -> None:
        if self._initialized:
            return
        self._subscriptions.extend(
            [
                await self._event_bus.subscribe(
                    "trade.position_opened",
                    self._on_position_opened,
                    group="knowledge-graph",
                ),
                await self._event_bus.subscribe(
                    "trade.position_closed",
                    self._on_position_closed,
                    group="knowledge-graph",
                ),
                await self._event_bus.subscribe(
                    "journal.mistake_recorded",
                    self._on_mistake_recorded,
                    group="knowledge-graph",
                ),
            ]
        )
        # The Redis bus resolves wildcard subscriptions against streams known
        # at bind time. Seed only the decision streams emitted by the runtime;
        # the wildcard remains the sole semantic subscription.
        known_topics = getattr(self._event_bus, "_known_topics", None)
        if known_topics is not None:
            known_topics.update(_SEMANTIC_DECISION_STREAMS)
        for topic in SEMANTIC_TOPICS:
            self._subscriptions.append(
                await self._event_bus.subscribe(
                    topic,
                    self._on_semantic_event,
                    group="knowledge-graph-semantic",
                    live_only=True,
                )
            )
        self._initialized = True
        logger.info(
            "KnowledgeGraphWriter initialized",
            extra={"aitos_extra": {"semantic_topics": SEMANTIC_TOPICS}},
        )

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            module_id=self.module_id,
            status=(
                ModuleStatus.HEALTHY if self._initialized else ModuleStatus.UNHEALTHY
            ),
            latency_ms=0.0,
            last_event_time=self._last_event_time,
            details={
                "writes_applied": self._writes_applied,
                "errors": self._errors,
                "semantic_topics": list(SEMANTIC_TOPICS),
            },
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        for sub in self._subscriptions:
            sub.cancel()
        self._subscriptions.clear()
        await self._driver.close()
        logger.info("KnowledgeGraphWriter shut down")

    async def emit_events(self) -> AsyncIterator[Event]:
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Event) -> EventResponse | None:
        return None

    async def update_symbol_correlation(
        self, symbol_a: str, symbol_b: str, coefficient: float, updated_at: str
    ) -> None:
        self._require_initialized()
        await self._run(
            CORRELATION_QUERY,
            symbol_a=symbol_a,
            symbol_b=symbol_b,
            coefficient=coefficient,
            updated_at=updated_at,
        )

    async def _on_position_opened(self, event: Event) -> EventResponse | None:
        payload = event.payload
        await self._run(
            CREATE_TRADE_QUERY,
            symbol=payload.get("symbol", ""),
            strategy_id=payload.get("strategy_id", ""),
            trade_id=payload.get("trade_id", ""),
            side=payload.get("side", ""),
            entry_price=payload.get("entry_price", 0.0),
            regime=payload.get("regime", "unknown"),
            state=payload.get("state", ""),
            entry_time=payload.get("entry_time", ""),
        )
        self._last_event_time = event.created_at
        return None

    async def _on_position_closed(self, event: Event) -> EventResponse | None:
        payload = event.payload
        await self._run(
            CLOSE_TRADE_QUERY,
            trade_id=payload.get("trade_id", ""),
            pnl=payload.get("pnl"),
            pnl_percent=payload.get("pnl_percent"),
            exit_price=payload.get("exit_price"),
            exit_reason=payload.get("exit_reason"),
            exit_time=payload.get("exit_time"),
            state=payload.get("state", ""),
        )
        self._last_event_time = event.created_at
        return None

    async def _on_mistake_recorded(self, event: Event) -> EventResponse | None:
        entry = event.payload
        trade_id = entry.get("trade_id")
        if not trade_id or not entry.get("mistakes"):
            return None
        for index, mistake_text in enumerate(entry["mistakes"]):
            await self._run(
                LINK_MISTAKE_QUERY,
                trade_id=trade_id,
                mistake_id=f"{event.event_id}:m:{index}",
                mistake_text=mistake_text,
                created_at=entry.get("created_at", ""),
            )
        self._last_event_time = event.created_at
        return None

    async def _on_semantic_event(self, event: Event) -> EventResponse | None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        await self._run(
            PROJECT_SEMANTIC_EVENT_QUERY,
            event_id=event.event_id,
            topic=event.topic,
            event_time=event.created_at,
            source_module=event.source_module,
            schema_version=str(payload.get("schema_version", "1")),
            payload_json=json.dumps(payload, default=str, separators=(",", ":")),
            symbol=_first(payload, "symbol", "instrument", "market_symbol"),
            strategy_id=_first(payload, "strategy_id", "strategy", "strategy_version"),
            model_id=_first(payload, "model_id", "model", "model_version"),
            policy_id=_first(payload, "policy_id", "policy", "policy_version"),
            trade_id=_first(payload, "trade_id", "position_id"),
            decision_id=_first(payload, "decision_id", "decision_idempotency_key"),
            regime=_first(payload, "regime", "market_regime", "regime_name"),
            risk_id=_first(payload, "risk_decision_id", "risk_id"),
            risk_state=_first(payload, "risk_state", "state"),
            risk_action=_first(payload, "risk_action", "action"),
            risk_score=_number(payload, "risk_score", "risk_confidence"),
            execution_id=_first(payload, "execution_id", "fill_id", "order_id"),
            execution_side=_first(payload, "side", "execution_side"),
            execution_status=_first(payload, "execution_status", "status"),
            execution_price=_number(payload, "execution_price", "fill_price", "price"),
            execution_quantity=_number(
                payload, "execution_quantity", "fill_quantity", "quantity"
            ),
            journey_id=_first(payload, "journey_id", "trade_journey_id"),
            journey_state=_first(payload, "journey_state", "journey_status", "state"),
            forecast_id=_first(payload, "forecast_id", "prediction_id"),
            forecast_probability=_number(
                payload, "probability", "forecast_probability", "confidence"
            ),
            forecast_target=_first(payload, "target", "forecast_target"),
            forecast_horizon=_first(payload, "horizon", "forecast_horizon"),
            outcome_id=_first(payload, "outcome_id", "result_id"),
            outcome_label=_first(payload, "outcome", "outcome_label", "label"),
            outcome_pnl=_number(payload, "pnl", "outcome_pnl"),
            run_id=_first(payload, "model_run_id", "run_id", "training_run_id"),
            dataset_id=_first(payload, "dataset_id", "dataset_version"),
            feature_set_version=_first(
                payload, "feature_set_version", "feature_version"
            ),
            artifact_id=_first(payload, "artifact_id", "model_artifact_id"),
            run_status=_first(payload, "run_status", "training_status", "status"),
            calibration_id=_first(payload, "calibration_id", "calibration_run_id"),
            calibration_method=_first(payload, "calibration_method", "method"),
            sample_count=_number(payload, "sample_count", "samples"),
            brier=_number(payload, "brier", "brier_score"),
            log_loss=_number(payload, "log_loss"),
            ece=_number(payload, "ece", "expected_calibration_error"),
            evidence=_evidence(payload, event.event_id),
        )
        self._last_event_time = event.created_at
        return None

    async def _run(self, query: str, **params: Any) -> None:
        try:
            async with self._driver.session() as session:
                await session.run(query, **params)
            self._writes_applied += 1
        except Exception as exc:  # noqa: BLE001
            self._errors += 1
            logger.error(
                "knowledge graph write failed",
                extra={"aitos_extra": {"error": str(exc)}},
            )

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError(
                "KnowledgeGraphWriter.initialize() must be called first"
            )
