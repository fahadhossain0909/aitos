"""KnowledgeGraphWriter — spec's Neo4j Knowledge Graph module, built
event-driven (subscribes to the Event Bus, same pattern as
``JournalSystem``/``RLFeedbackLoop``) so the graph grows automatically as
real trades happen — no separate ETL/batch job needed.

Graph shape:

    (:Trade {id, side, entry_price, pnl, regime, state, ...})
      -[:ON_SYMBOL]->        (:Symbol {name})
      -[:USED_STRATEGY]->    (:Strategy {id})
      -[:HAD_MISTAKE]->      (:Mistake {text})
    (:Symbol)-[:CORRELATED_WITH {coefficient, updated_at}]->(:Symbol)

The driver is injected (constructor arg) rather than created internally,
matching the ``EventBus``/repository pattern elsewhere in this codebase —
tests use a fake driver double that records the Cypher/parameters sent
rather than needing a real Neo4j server (see ``docker-compose.yml`` for
running one for real).
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
    def session(
        self,
    ) -> Any: ...  # returns an async context manager yielding a GraphSession

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


class KnowledgeGraphWriter(AITOSModule):
    def __init__(self, event_bus: EventBus, driver: GraphDriver) -> None:
        self._event_bus = event_bus
        self._driver = driver
        self._initialized = False
        self._subscriptions: list[Subscription] = []
        self._writes_applied = 0
        self._errors = 0
        self._last_event_time: str | None = None

    # -- AITOSModule contract -------------------------------------------------

    @property
    def module_id(self) -> str:
        return "knowledge-graph-writer"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: dict[str, Any]) -> None:
        if self._initialized:
            return
        self._subscriptions.append(
            await self._event_bus.subscribe(
                "trade.position_opened",
                self._on_position_opened,
                group="knowledge-graph",
            )
        )
        self._subscriptions.append(
            await self._event_bus.subscribe(
                "trade.position_closed",
                self._on_position_closed,
                group="knowledge-graph",
            )
        )
        self._subscriptions.append(
            await self._event_bus.subscribe(
                "journal.mistake_recorded",
                self._on_mistake_recorded,
                group="knowledge-graph",
            )
        )
        self._initialized = True
        logger.info("KnowledgeGraphWriter initialized")

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            module_id=self.module_id,
            status=(
                ModuleStatus.HEALTHY if self._initialized else ModuleStatus.UNHEALTHY
            ),
            latency_ms=0.0,
            last_event_time=self._last_event_time,
            details={"writes_applied": self._writes_applied, "errors": self._errors},
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

    # -- Public API ---------------------------------------------------------------

    async def update_symbol_correlation(
        self, symbol_a: str, symbol_b: str, coefficient: float, updated_at: str
    ) -> None:
        """Direct call (not event-driven) — pearson correlation between two
        symbols isn't a single trade's business, it's computed from market
        data by ``SymbolCorrelationUpdater`` and pushed here."""
        self._require_initialized()
        await self._run(
            CORRELATION_QUERY,
            symbol_a=symbol_a,
            symbol_b=symbol_b,
            coefficient=coefficient,
            updated_at=updated_at,
        )

    # -- Event handlers -------------------------------------------------------------

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
            return None  # daily/weekly review entries have no trade_id to attach to
        for mistake_text in entry["mistakes"]:
            await self._run(
                LINK_MISTAKE_QUERY,
                trade_id=trade_id,
                mistake_text=mistake_text,
                created_at=entry.get("created_at", ""),
            )
        self._last_event_time = event.created_at
        return None

    # -- Internals --------------------------------------------------------------

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
