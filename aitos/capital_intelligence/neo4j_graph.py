"""Optional Neo4j persistence for evidence-first CFEI entity graphs."""

from __future__ import annotations

from typing import Any, Iterable

from .contracts import CapitalEvent, EntityEvidence, EntityLabel


class Neo4jEntityGraphStore:
    """Parameterized Cypher; event edges never imply beneficial ownership."""

    def __init__(self, driver: Any, *, database: str | None = None):
        self.driver = driver
        self.database = database

    async def ensure_schema(self) -> None:
        async with self.driver.session(database=self.database) as session:
            await session.run(
                "CREATE CONSTRAINT cfei_entity_id IF NOT EXISTS "
                "FOR (n:CFEIEntity) REQUIRE n.entity_id IS UNIQUE"
            )
            await session.run(
                "CREATE CONSTRAINT cfei_label_key IF NOT EXISTS "
                "FOR (n:CFEILabel) REQUIRE n.label_id IS UNIQUE"
            )
            await session.run(
                "CREATE CONSTRAINT cfei_evidence_key IF NOT EXISTS "
                "FOR (n:CFEIEvidence) REQUIRE n.evidence_id IS UNIQUE"
            )
            await session.run(
                "CREATE CONSTRAINT cfei_event_key IF NOT EXISTS "
                "FOR (n:CFEIEvent) REQUIRE n.event_key IS UNIQUE"
            )

    async def upsert_events(self, events: Iterable[CapitalEvent]) -> int:
        """Persist immutable event observations and address references idempotently."""
        rows = []
        for event in events:
            actor = f"{event.venue}:{event.actor_id}" if event.actor_id else None
            counterparty = (
                f"{event.venue}:{event.counterparty_id}"
                if event.counterparty_id
                else None
            )
            rows.append(
                {
                    "event_key": event.deduplication_key,
                    "event_id": event.event_id,
                    "source_id": event.source_id,
                    "event_time": event.event_time.isoformat(),
                    "observed_time": event.observed_time.isoformat(),
                    "market": event.market,
                    "venue": event.venue,
                    "instrument": event.instrument,
                    "event_type": event.event_type,
                    "actor_entity_id": actor,
                    "counterparty_entity_id": counterparty,
                    "transaction_id": event.transaction_id,
                    "block_number": event.block_number,
                    "finality": event.finality,
                    "quantity": event.quantity,
                    "unit": event.unit,
                    "usd_value": event.usd_value,
                }
            )
        if not rows:
            return 0
        query = """UNWIND $rows AS row
        MERGE (v:CFEIEvent {event_key: row.event_key})
        SET v.event_id=row.event_id, v.source_id=row.source_id,
            v.event_time=row.event_time, v.observed_time=row.observed_time,
            v.market=row.market, v.venue=row.venue, v.instrument=row.instrument,
            v.event_type=row.event_type, v.transaction_id=row.transaction_id,
            v.block_number=row.block_number, v.finality=row.finality,
            v.quantity=row.quantity, v.unit=row.unit, v.usd_value=row.usd_value
        FOREACH (_ IN CASE WHEN row.actor_entity_id IS NULL THEN [] ELSE [1] END |
            MERGE (a:CFEIEntity {entity_id: row.actor_entity_id})
            MERGE (a)-[:OBSERVED_EVENT]->(v))
        FOREACH (_ IN CASE WHEN row.counterparty_entity_id IS NULL THEN [] ELSE [1] END |
            MERGE (c:CFEIEntity {entity_id: row.counterparty_entity_id})
            MERGE (v)-[:REFERENCES_COUNTERPARTY]->(c))"""
        async with self.driver.session(database=self.database) as session:
            await session.run(query, rows=rows)
        return len(rows)

    async def upsert_labels(self, labels: Iterable[EntityLabel]) -> int:
        rows = [
            {
                "entity_id": item.entity_id,
                "label": item.label,
                "category": item.category,
                "source": item.source,
                "verified": item.verified,
                "valid_from": item.valid_from.isoformat(),
                "valid_until": (
                    item.valid_until.isoformat() if item.valid_until else None
                ),
                "label_id": "|".join(
                    (
                        item.entity_id,
                        item.category,
                        item.label,
                        item.source,
                        item.valid_from.isoformat(),
                    )
                ),
            }
            for item in labels
        ]
        if not rows:
            return 0
        query = """UNWIND $rows AS row
        MERGE (e:CFEIEntity {entity_id: row.entity_id})
        MERGE (l:CFEILabel {label_id: row.label_id})
        SET l.label=row.label, l.category=row.category, l.source=row.source,
            l.verified=row.verified, l.valid_from=row.valid_from,
            l.valid_until=row.valid_until
        MERGE (e)-[:HAS_LABEL]->(l)"""
        async with self.driver.session(database=self.database) as session:
            await session.run(query, rows=rows)
        return len(rows)

    async def upsert_evidence(self, evidence: Iterable[EntityEvidence]) -> int:
        rows = [
            {
                "subject_id": item.subject_id,
                "object_id": item.object_id,
                "relation": item.relation,
                "evidence_type": item.evidence_type,
                "evidence_ref": item.evidence_ref,
                "observed_at": item.observed_at.isoformat(),
                "confidence": item.confidence,
                "status": item.status,
                "evidence_id": "|".join(
                    (
                        item.subject_id,
                        item.object_id,
                        item.relation,
                        item.evidence_ref,
                    )
                ),
            }
            for item in evidence
        ]
        if not rows:
            return 0
        query = """UNWIND $rows AS row
        MERGE (a:CFEIEntity {entity_id: row.subject_id})
        MERGE (b:CFEIEntity {entity_id: row.object_id})
        MERGE (v:CFEIEvidence {evidence_id: row.evidence_id})
        SET v.relation=row.relation, v.evidence_type=row.evidence_type,
            v.evidence_ref=row.evidence_ref, v.observed_at=row.observed_at,
            v.confidence=row.confidence, v.status=row.status
        MERGE (a)-[:HAS_EVIDENCE]->(v)
        MERGE (v)-[:ABOUT_ENTITY]->(b)"""
        async with self.driver.session(database=self.database) as session:
            await session.run(query, rows=rows)
        return len(rows)

    async def close(self) -> None:
        await self.driver.close()
