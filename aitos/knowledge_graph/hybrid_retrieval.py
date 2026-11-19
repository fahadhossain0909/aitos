"""Dependency-free hybrid ranking for retrieved quantitative memories."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_TOKEN = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class RetrievalWeights:
    lexical: float = 0.35
    semantic: float = 0.40
    recency: float = 0.10
    outcome: float = 0.15

    def __post_init__(self):
        values = (self.lexical, self.semantic, self.recency, self.outcome)
        if any(not math.isfinite(v) or v < 0 for v in values) or sum(values) <= 0:
            raise ValueError("weights must be finite, non-negative, and not all zero")


def _tokens(value: str) -> set[str]:
    return set(_TOKEN.findall(value.lower()))


def _lexical_score(query: str, document: str) -> float:
    q, d = _tokens(query), _tokens(document)
    return len(q & d) / len(q | d) if q and d else 0.0


def _recency_score(event_time: Any, now: datetime, half_life_days: float) -> float:
    if not math.isfinite(half_life_days) or half_life_days <= 0:
        raise ValueError("half_life_days must be finite and positive")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if isinstance(event_time, str):
        try:
            event_time = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    if not isinstance(event_time, datetime):
        return 0.0
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)
    age = max(0.0, (now - event_time).total_seconds() / 86400)
    return math.exp(-math.log(2) * age / half_life_days)


def _outcome_score(row: dict[str, Any], now: datetime) -> float:
    # A closed-trade outcome is unavailable to a historical decision until its
    # outcome timestamp. Treat future outcomes as unknown to prevent leakage.
    available_at = row.get("outcome_available_at") or row.get("closed_at")
    if isinstance(available_at, str):
        try:
            available_at = datetime.fromisoformat(available_at.replace("Z", "+00:00"))
        except ValueError:
            available_at = None
    if isinstance(available_at, datetime):
        if available_at.tzinfo is None:
            available_at = available_at.replace(tzinfo=timezone.utc)
        comparison_now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        if available_at > comparison_now:
            return 0.5
    try:
        pnl = float(row.get("pnl"))
    except (TypeError, ValueError):
        return 0.5
    if not math.isfinite(pnl):
        return 0.5
    return 0.5 + 0.5 * math.tanh(pnl)


def rank_hybrid_memories(
    query: str,
    rows: Iterable[dict[str, Any]],
    *,
    semantic_scores: dict[str, float] | None = None,
    weights: RetrievalWeights | None = None,
    now: datetime | None = None,
    half_life_days: float = 30.0,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Rerank retrieved candidates; scores are context metadata, never signals."""
    if limit < 1:
        raise ValueError("limit must be positive")
    if not math.isfinite(half_life_days) or half_life_days <= 0:
        raise ValueError("half_life_days must be finite and positive")
    weights = weights or RetrievalWeights()
    now = now or datetime.now(timezone.utc)
    semantic_scores = semantic_scores or {}
    total = weights.lexical + weights.semantic + weights.recency + weights.outcome
    ranked = []
    for source in rows:
        row = dict(source)
        event_id = str(row.get("event_id", ""))
        document = " ".join(
            str(row.get(k, "") or "")
            for k in ("topic", "symbol", "regime", "strategy_id", "outcome")
        )
        try:
            semantic = float(semantic_scores.get(event_id, 0.0))
        except (TypeError, ValueError):
            semantic = 0.0
        if not math.isfinite(semantic):
            semantic = 0.0
        components = {
            "lexical": _lexical_score(query, document),
            "semantic": min(1.0, max(0.0, semantic)),
            "recency": _recency_score(row.get("event_time"), now, half_life_days),
            "outcome": _outcome_score(row, now),
        }
        row["retrieval_score"] = (
            sum(getattr(weights, k) * v for k, v in components.items()) / total
        )
        row["retrieval_components"] = components
        row["retrieval_provenance"] = {
            "source": "knowledge_graph",
            "event_id": event_id or None,
            "score_version": "hybrid-v1",
        }
        ranked.append(row)
    ranked.sort(
        key=lambda r: (
            -r["retrieval_score"],
            str(r.get("event_time", "")),
            str(r.get("event_id", "")),
        )
    )
    return ranked[:limit]
