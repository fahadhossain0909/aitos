from .backfill import ClickHouseNeo4jBackfill
from .correlation_updater import SymbolCorrelationUpdater
from .retrieval import GraphContextRetriever
from .schema import ensure_schema, schema_statements
from .writer import GraphDriver, GraphSession, KnowledgeGraphWriter

_ORIGINAL_INITIALIZE = KnowledgeGraphWriter.initialize
_SEMANTIC_TOPICS = (
    "decision.trade_candidate",
    "decision.generated",
)


async def _initialize_with_semantic_topic_bindings(self, config):
    already_initialized = getattr(self, "_initialized", False)
    await _ORIGINAL_INITIALIZE(self, config)
    if already_initialized:
        return

    for topic in _SEMANTIC_TOPICS:
        subscription = await self._event_bus.subscribe(
            topic,
            self._on_semantic_event,
            group=f"knowledge-graph-semantic-{topic}",
            live_only=True,
        )
        self._subscriptions.append(subscription)


KnowledgeGraphWriter.initialize = _initialize_with_semantic_topic_bindings

__all__ = [
    "ClickHouseNeo4jBackfill",
    "GraphContextRetriever",
    "GraphDriver",
    "GraphSession",
    "KnowledgeGraphWriter",
    "SymbolCorrelationUpdater",
    "ensure_schema",
    "schema_statements",
]
