"""Read-only graph retrieval for AI/contextual decision features."""

from __future__ import annotations

from typing import Any


SIMILAR_CASES_QUERY = """
MATCH (e:KnowledgeEvent)
OPTIONAL MATCH (e)-[:ABOUT_SYMBOL]->(s:Symbol)
OPTIONAL MATCH (e)-[:OCCURRED_IN_REGIME]->(r:MarketRegime)
OPTIONAL MATCH (e)-[:INVOLVES_STRATEGY]->(st:Strategy)
OPTIONAL MATCH (e)-[:PRODUCED_BY_MODEL]->(m:Model)
OPTIONAL MATCH (e)-[:REFERENCES_OUTCOME]->(o:Outcome)
WITH e, s, r, st, m, o,
     (CASE WHEN $symbol <> '' AND s.name = $symbol THEN 5 ELSE 0 END +
      CASE WHEN $regime <> '' AND r.name = $regime THEN 3 ELSE 0 END +
      CASE WHEN $strategy_id <> '' AND st.id = $strategy_id THEN 2 ELSE 0 END +
      CASE WHEN $model_id <> '' AND m.id = $model_id THEN 1 ELSE 0 END) AS score
WHERE score > 0
RETURN e.id AS event_id, e.topic AS topic, e.event_time AS event_time,
       s.name AS symbol, r.name AS regime, st.id AS strategy_id,
       m.id AS model_id, o.label AS outcome, o.pnl AS pnl, score
ORDER BY score DESC, e.event_time DESC
LIMIT $limit
"""


class GraphContextRetriever:
    """Returns compact, non-authoritative graph context for downstream models."""

    def __init__(self, driver: Any) -> None:
        self._driver = driver

    async def similar_cases(
        self,
        *,
        symbol: str = "",
        regime: str = "",
        strategy_id: str = "",
        model_id: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        async with self._driver.session() as session:
            result = await session.run(
                SIMILAR_CASES_QUERY,
                symbol=symbol,
                regime=regime,
                strategy_id=strategy_id,
                model_id=model_id,
                limit=limit,
            )
            rows = await result.data()
        return rows
