"""Optional Neo4j persistence for evidence-first CFEI entity graphs."""
from __future__ import annotations
from typing import Any, Iterable
from .contracts import EntityEvidence, EntityLabel

class Neo4jEntityGraphStore:
    """Uses parameterized Cypher; candidate edges remain explicitly non-ownership."""
    def __init__(self, driver: Any, *, database: str | None = None):
        self.driver, self.database = driver, database

    async def ensure_schema(self) -> None:
        async with self.driver.session(database=self.database) as session:
            await session.run("CREATE CONSTRAINT cfei_entity_id IF NOT EXISTS FOR (n:CFEIEntity) REQUIRE n.entity_id IS UNIQUE")
            await session.run("CREATE CONSTRAINT cfei_label_key IF NOT EXISTS FOR (n:CFEILabel) REQUIRE n.label_id IS UNIQUE")
            await session.run("CREATE CONSTRAINT cfei_evidence_key IF NOT EXISTS FOR (n:CFEIEvidence) REQUIRE n.evidence_id IS UNIQUE")

    async def upsert_labels(self, labels: Iterable[EntityLabel]) -> int:
        rows = [{"entity_id": x.entity_id, "label": x.label, "category": x.category,
                 "source": x.source, "verified": x.verified,
                 "valid_from": x.valid_from.isoformat(),
                 "valid_until": x.valid_until.isoformat() if x.valid_until else None,
                 "label_id": "|".join((x.entity_id,x.category,x.label,x.source,x.valid_from.isoformat()))}
                for x in labels]
        if not rows: return 0
        query = """UNWIND $rows AS row
        MERGE (e:CFEIEntity {entity_id: row.entity_id})
        MERGE (l:CFEILabel {label_id: row.label_id})
        SET l.label=row.label, l.category=row.category, l.source=row.source,
            l.verified=row.verified, l.valid_from=row.valid_from, l.valid_until=row.valid_until
        MERGE (e)-[:HAS_LABEL]->(l)"""
        async with self.driver.session(database=self.database) as session:
            await session.run(query, rows=rows)
        return len(rows)

    async def upsert_evidence(self, evidence: Iterable[EntityEvidence]) -> int:
        rows = [{"subject_id": x.subject_id, "object_id": x.object_id,
                 "relation": x.relation, "evidence_type": x.evidence_type,
                 "evidence_ref": x.evidence_ref, "observed_at": x.observed_at.isoformat(),
                 "confidence": x.confidence, "status": x.status,
                 "evidence_id": "|".join((x.subject_id,x.object_id,x.relation,x.evidence_ref))}
                for x in evidence]
        if not rows: return 0
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
