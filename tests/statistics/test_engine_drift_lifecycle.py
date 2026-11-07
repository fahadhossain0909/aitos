import asyncio

import pytest

from aitos.statistics.engine import AStatEngine
from aitos.statistics.governance import PromotionPolicy
from aitos.statistics.governance_runtime import StatisticalGovernanceRuntime
from aitos.statistics.registry import StatisticalModelRegistry


@pytest.mark.asyncio
async def test_a_stat_engine_starts_and_stops_managed_drift_monitor(tmp_path):
    registry = StatisticalModelRegistry(tmp_path / "models.sqlite3")
    runtime = StatisticalGovernanceRuntime(
        registry, policy=PromotionPolicy(min_observations=1)
    )
    engine = AStatEngine()
    alerted = asyncio.Event()
    reference = {"volatility": [float(i) for i in range(100)]}
    current = {"volatility": [float(i + 1000) for i in range(100)]}

    async def batch_provider():
        return reference, current

    async def alert_callback(action):
        assert action.status in {"WARNING", "CRITICAL"}
        alerted.set()

    task = await engine.start_drift_monitor(
        runtime,
        batch_provider,
        interval_seconds=1.0,
        alert_callback=alert_callback,
    )
    await asyncio.wait_for(alerted.wait(), timeout=2.0)
    await engine.stop_drift_monitor()
    assert task.done()
    await engine.stop_drift_monitor()
