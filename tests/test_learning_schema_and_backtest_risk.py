import asyncio
from math import inf, nan

from aitos.backtest.cli import _initial_risk_state
from aitos.data.repository import CREATE_LEARNING_EXPERIENCES, MarketDataRepository


def test_backtest_risk_requires_explicit_positive_finite_initial_risk():
    assert _initial_risk_state({"initial_risk_usd": 12.5}) == {
        "risk_amount_usd": 12.5
    }
    assert _initial_risk_state({"risk_amount_usd": 8}) == {"risk_amount_usd": 8.0}
    assert _initial_risk_state({"risk_amount_usd": 0, "initial_risk_usd": -1}) == {}
    assert _initial_risk_state({"initial_risk_usd": nan}) == {}
    assert _initial_risk_state({"initial_risk_usd": inf}) == {}
    # Position notional and PnL must never be treated as initial risk.
    assert _initial_risk_state({"position_size_usd": 500, "pnl": 25}) == {}


def test_runtime_learning_table_declares_ingestion_timestamp():
    assert "ingested_at DateTime64(3, 'UTC') DEFAULT timestamp" in (
        CREATE_LEARNING_EXPERIENCES
    )


def test_runtime_learning_schema_migrates_existing_table():
    class FakeClient:
        def __init__(self):
            self.commands = []

        async def command(self, sql):
            self.commands.append(sql)

    repository = MarketDataRepository.__new__(MarketDataRepository)
    repository._initialized = True
    repository._client = FakeClient()

    asyncio.run(repository.ensure_learning_experience_schema())

    assert len(repository._client.commands) == 2
    assert repository._client.commands[0] == CREATE_LEARNING_EXPERIENCES
    assert "ADD COLUMN IF NOT EXISTS ingested_at" in repository._client.commands[1]
