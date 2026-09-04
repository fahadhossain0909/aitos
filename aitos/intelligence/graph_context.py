"""Neo4j historical context adapter for the contextual decision layer."""

from __future__ import annotations

import os
from typing import Any

from aitos.knowledge_graph.retrieval import GraphContextRetriever

_DRIVER: Any | None = None
_RETRIEVER: GraphContextRetriever | None = None


def _get_retriever() -> GraphContextRetriever | None:
    global _DRIVER, _RETRIEVER
    if _RETRIEVER is not None:
        return _RETRIEVER
    uri = os.getenv("NEO4J_URI", "").strip()
    user = os.getenv("NEO4J_USER", "neo4j").strip()
    password = os.getenv("NEO4J_PASSWORD", "")
    if not uri or not password:
        return None
    try:
        from neo4j import AsyncGraphDatabase

        _DRIVER = AsyncGraphDatabase.driver(uri, auth=(user, password))
        _RETRIEVER = GraphContextRetriever(_DRIVER)
        return _RETRIEVER
    except Exception:
        return None


def _directional_score(rows: list[dict[str, Any]], direction: str) -> float:
    """Convert resolved historical cases to the standard 0..10 evidence scale."""
    if not rows:
        return 5.0
    positives = negatives = total = 0.0
    wanted = "long" if direction == "long" else "short"
    for row in rows:
        outcome = str(row.get("outcome") or "").lower()
        pnl = row.get("pnl")
        weight = max(0.1, float(row.get("score") or 1.0))
        if outcome in {"win", "success", "profitable", "positive", "long"}:
            positives += weight
        elif outcome in {"loss", "failure", "negative", "short"}:
            negatives += weight
        elif isinstance(pnl, (int, float)):
            (positives if float(pnl) > 0 else negatives) += weight
        total += weight
    if total <= 0:
        return 5.0
    # Rows labelled by outcome are directional only when the query direction is
    # also represented. Otherwise use the resolved win/loss balance as a prior.
    edge = (positives - negatives) / total
    if wanted == "short":
        edge = -edge
    return round(max(0.0, min(10.0, 5.0 + 5.0 * edge)), 4)


async def retrieve_graph_context(*, symbol: str, regime: str, strategy_id: str = "", model_id: str = "", limit: int = 12) -> dict[str, Any]:
    """Best-effort graph context. Failure always degrades to unavailable context."""
    retriever = _get_retriever()
    if retriever is None:
        return {"available": False, "cases": [], "score": 5.0}
    try:
        rows = await retriever.similar_cases(symbol=symbol, regime=regime, strategy_id=strategy_id, model_id=model_id, limit=limit)
        return {
            "available": bool(rows),
            "cases": rows,
            "score": _directional_score(rows, "long"),
            "case_count": len(rows),
        }
    except Exception:
        return {"available": False, "cases": [], "score": 5.0}
