from datetime import datetime, timezone

import pytest

from aitos.knowledge_graph.hybrid_retrieval import (
    RetrievalWeights,
    rank_hybrid_memories,
)


def test_hybrid_ranking_uses_semantics_recency_and_outcome():
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    rows = [
        {
            "event_id": "old",
            "topic": "decision",
            "symbol": "BTCUSDT",
            "regime": "range",
            "event_time": "2026-08-01T00:00:00Z",
            "pnl": -0.2,
        },
        {
            "event_id": "fresh",
            "topic": "decision",
            "symbol": "BTCUSDT",
            "regime": "trend",
            "event_time": "2026-11-18T00:00:00Z",
            "pnl": 0.2,
        },
    ]
    result = rank_hybrid_memories(
        "BTCUSDT trend", rows, semantic_scores={"old": 0.1, "fresh": 0.95}, now=now
    )
    assert result[0]["event_id"] == "fresh"
    assert set(result[0]["retrieval_components"]) == {
        "lexical",
        "semantic",
        "recency",
        "outcome",
    }
    assert result[0]["retrieval_provenance"]["score_version"] == "hybrid-v1"


def test_hybrid_ranker_clamps_scores_and_defaults_missing_outcome():
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    result = rank_hybrid_memories(
        "ETH", [{"event_id": "e", "symbol": "ETH"}], semantic_scores={"e": 99}, now=now
    )
    assert result[0]["retrieval_components"]["semantic"] == 1.0
    assert result[0]["retrieval_components"]["outcome"] == 0.5


def test_hybrid_ranker_validates_inputs():
    with pytest.raises(ValueError):
        RetrievalWeights(-1, 1, 1, 1)
    with pytest.raises(ValueError):
        rank_hybrid_memories("x", [], limit=0)
    with pytest.raises(ValueError):
        rank_hybrid_memories("x", [], half_life_days=0)


def test_future_outcome_is_not_used_for_historical_retrieval():
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    result = rank_hybrid_memories(
        "BTC",
        [
            {
                "event_id": "future",
                "symbol": "BTC",
                "pnl": 0.9,
                "closed_at": "2026-11-20T00:00:00Z",
            }
        ],
        now=now,
    )
    assert result[0]["retrieval_components"]["outcome"] == 0.5


def test_naive_clock_and_non_finite_half_life_are_handled_safely():
    result = rank_hybrid_memories(
        "BTC",
        [{"event_id": "e", "symbol": "BTC"}],
        now=datetime(2026, 11, 19),
        half_life_days=30,
    )
    assert 0 <= result[0]["retrieval_score"] <= 1
    with pytest.raises(ValueError):
        rank_hybrid_memories("BTC", [], half_life_days=float("inf"))
