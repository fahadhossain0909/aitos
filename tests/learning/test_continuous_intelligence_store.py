import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aitos.learning.clickhouse_store import ClickHouseExperienceStore
from aitos.learning.continuous_intelligence import ContinuousIntelligenceEngine


class FakeClickHouse:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def query(self, query, parameters):
        self.calls.append((query, parameters))
        return SimpleNamespace(result_rows=self.rows)


def _row(metadata=None):
    return (
        "00000000-0000-0000-0000-000000000001",
        datetime(2026, 11, 1, tzinfo=timezone.utc),
        "paper",
        "BTCUSDT",
        "LONG",
        "closed",
        1.25,
        json.dumps({"cvd": 0.8}),
        json.dumps({"regime": "trend"}),
        json.dumps({"risk_amount_usd": 10.0}),
        "momentum-v2",
        "model-v3",
        json.dumps({"reward_unit": "r_multiple", **(metadata or {})}),
    )


def test_closed_experience_query_is_bounded_and_parameterized():
    client = FakeClickHouse([_row()])
    store = ClickHouseExperienceStore(client)
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    end = datetime(2026, 11, 2, tzinfo=timezone.utc)

    records = store.query_closed_experiences(
        start, end, sources=("paper", "backtest"), limit=250
    )

    assert len(records) == 1
    assert records[0]["metadata"]["reward_unit"] == "r_multiple"
    query, params = client.calls[0]
    assert "outcome = 'closed'" in query
    assert "source IN {sources:Array(String)}" in query
    assert params["sources"] == ["backtest", "paper"]
    assert params["limit"] == 250


def test_closed_experience_query_rejects_invalid_window_and_sources():
    store = ClickHouseExperienceStore(FakeClickHouse([]))
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="after start"):
        store.query_closed_experiences(now, now)
    with pytest.raises(ValueError, match="timezone-aware"):
        store.query_closed_experiences(now.replace(tzinfo=None), now)
    with pytest.raises(ValueError, match="sources"):
        store.query_closed_experiences(
            now, now + timedelta(minutes=1), sources=("unknown",)
        )


def test_malformed_experience_json_fails_closed():
    row = list(_row())
    row[12] = "[]"
    store = ClickHouseExperienceStore(FakeClickHouse([tuple(row)]))
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="metadata must decode"):
        store.query_closed_experiences(now, datetime(2026, 11, 2, tzinfo=timezone.utc))


def test_analyzer_uses_canonical_experience_store_contract():
    class Store:
        def query_closed_experiences(self, start, end, *, sources=None, limit=0):
            assert start < end
            assert sources == ("paper",)
            assert limit == 10
            return [
                {
                    "experience_id": "stored-1",
                    "source": "paper",
                    "symbol": "BTCUSDT",
                    "decision": "LONG",
                    "outcome": "closed",
                    "reward": 0.75,
                    "strategy_version": "momentum-v2",
                    "market_state": {"regime": "trend"},
                    "metadata": {"reward_unit": "r_multiple"},
                }
            ]

    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    end = datetime(2026, 11, 1, tzinfo=timezone.utc)
    report = ContinuousIntelligenceEngine().analyze_store(
        Store(), start, end, sources=("paper",), limit=10
    )
    assert report.eligible_trades == 1
    assert report.performance_by_strategy_regime[0].mean_realized_r == 0.75
