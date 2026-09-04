"""Event-driven Neo4j knowledge graph for AITOS.

Neo4j is deliberately a *semantic relationship layer*, not a high-frequency
market-data store. Redis/EventBus is the live transport, ClickHouse is the
canonical durable analytical store, and this writer projects selected,
low-volume decision/intelligence/trade semantics into a graph.

The graph is useful for questions such as:
- which strategies/market regimes produced similar outcomes?
- which symbols co-move and which lead/lag each other?
- which evidence/features were attached to a decision?
- which risk decisions or journey states preceded an outcome?
- which model/policy versions are associated with good/bad outcomes?
- which mistakes recur for a strategy or market regime?

High-frequency ticks, order-book deltas and raw feature streams remain out of
Neo4j. They stay in Redis/ClickHouse and are referenced by IDs when needed.
"""

from __future__ import annotations

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
CREATE (t:Trade {
    id: $trade_id, side: $side, entry_price: $entry_price, regime: $regime,
    state: $state, opened_at: $entry_time
})
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
CREATE (m:Mistake {text: $mistake_text, recorded_at: $created_at})
MERGE (t)-[:HAD_MISTAKE]->(m)
"""

CORRELATION_QUERY = """
MERGE (a:Symbol {name: $symbol_a})
MERGE (b:Symbol {name: $symbol_b})
MERGE (a)-[r:CORRELATED_WITH]->(b)
SET r.coefficient = $coefficient, r.updated_at = $updated_at
"""

# These are deliberately semantic/decision topics. Market ticks and L2
# streams are excluded so Neo4j cannot become a hidden high-frequency sink.
SEMANTIC_TOPICS = (
    "decision.*",
    "risk.*",
    "scanner.*",
    "statistics.*",
    "intelligence.*",
    "journey.*",
    "execution.*",
)

PROJECT_SEMANTIC_EVENT_QUERY = """
MERGE (e:KnowledgeEvent {id: $event_id})
SET e.topic = $topic,
    e.event_time = $event_time,
    e.source_module = $source_module,
    e.schema_version = $schema_version,
    e.payload_json = $payload_json
WITH e
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
"""


def _first(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return ""


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
        return "2.0.0"

    async def initialize(self, config: dict[str, Any]) -> None:
        if self._initialized:
            return
        self._subscriptions.extend(
            [
                await self._event_bus.subscribe(
                    "trade.position_opened", self._on_position_opened, group="knowledge-graph"
                ),
                await self._event_bus.subscribe(
                    "trade.position_closed", self._on_position_closed, group="knowledge-graph"
                ),
                await self._event_bus.subscribe(
                    "journal.mistake_recorded", self._on_mistake_recorded, group="knowledge-graph"
                ),
            ]
        )
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
            status=ModuleStatus.HEALTHY if self._initialized else ModuleStatus.UNHEALTHY,
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
        trade_dict = event.payload
        await self._run(
            CREATE_TRADE_QUERY,
            symbol=trade_dict.get("symbol", ""),
            strategy_id=trade_dict.get("strategy_id", ""),
            trade_id=trade_dict.get("trade_id", ""),
            side=trade_dict.get("side", ""),
            entry_price=trade_dict.get("entry_price", 0.0),
            regime=trade_dict.get("regime", "unknown"),
            state=trade_dict.get("state", ""),
            entry_time=trade_dict.get("entry_time", ""),
        )
        self._last_event_time = event.created_at
        return None

    async def _on_position_closed(self, event: Event) -> EventResponse | None:
        trade_dict = event.payload
        await self._run(
            CLOSE_TRADE_QUERY,
            trade_id=trade_dict.get("trade_id", ""),
            pnl=trade_dict.get("pnl"),
            pnl_percent=trade_dict.get("pnl_percent"),
            exit_price=trade_dict.get("exit_price"),
            exit_reason=trade_dict.get("exit_reason"),
            exit_time=trade_dict.get("exit_time"),
            state=trade_dict.get("state", ""),
        )
        self._last_event_time = event.created_at
        return None

    async def _on_mistake_recorded(self, event: Event) -> EventResponse | None:
        entry = event.payload
        trade_id = entry.get("trade_id")
        if not trade_id or not entry.get("mistakes"):
            return None
        for mistake_text in entry["mistakes"]:
            await self._run(
                LINK_MISTAKE_QUERY,
                trade_id=trade_id,
                mistake_text=mistake_text,
                created_at=entry.get("created_at", ""),
            )
        self._last_event_time = event.created_at
        return None

    async def _on_semantic_event(self, event: Event) -> EventResponse | None:
        """Project a bounded semantic event; never persist raw market streams."""
        payload = event.payload if isinstance(event.payload, dict) else {}
        import json

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
