"""Neo4j schema hardening for the AITOS knowledge graph."""

from __future__ import annotations

from typing import Any

CONSTRAINTS = (
    ("trade_id", "Trade", "id"),
    ("decision_id", "Decision", "id"),
    ("risk_decision_id", "RiskDecision", "id"),
    ("execution_id", "Execution", "id"),
    ("journey_id", "TradeJourney", "id"),
    ("forecast_id", "Forecast", "id"),
    ("outcome_id", "Outcome", "id"),
    ("model_id", "Model", "id"),
    ("policy_id", "Policy", "id"),
    ("model_run_id", "ModelRun", "id"),
    ("calibration_run_id", "CalibrationRun", "id"),
    ("knowledge_event_id", "KnowledgeEvent", "id"),
    ("evidence_id", "Evidence", "id"),
    ("strategy_id", "Strategy", "id"),
)

INDEXES = (
    ("symbol_name", "Symbol", "name"),
    ("regime_name", "MarketRegime", "name"),
    ("knowledge_event_topic", "KnowledgeEvent", "topic"),
    ("knowledge_event_time", "KnowledgeEvent", "event_time"),
)


def schema_statements() -> tuple[str, ...]:
    statements: list[str] = []
    for name, label, prop in CONSTRAINTS:
        statements.append(
            f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
        )
    for name, label, prop in INDEXES:
        statements.append(
            f"CREATE INDEX {name} IF NOT EXISTS FOR (n:{label}) ON (n.{prop})"
        )
    return tuple(statements)


async def ensure_schema(driver: Any) -> None:
    """Create idempotent constraints/indexes; safe to call during maintenance."""
    async with driver.session() as session:
        for statement in schema_statements():
            await session.run(statement)
