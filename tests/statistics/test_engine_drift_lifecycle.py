import asyncio
import threading

import pytest

from aitos.statistics.engine import AStatEngine
from aitos.statistics.hawkes import fit_marked_hawkes_mle
from aitos.statistics.governance_runtime import StatisticalGovernanceRuntime
from aitos.statistics.registry import StatisticalModelRegistry


@pytest.mark.asyncio
async def test_a_stat_engine_starts_and_stops_managed_drift_monitor(tmp_path):
    registry = StatisticalModelRegistry(tmp_path / "models.sqlite3")
    runtime = StatisticalGovernanceRuntime(registry)
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


def test_a_stat_engine_routes_live_marked_events_to_registered_hawkes_filter():
    engine = AStatEngine()
    fit = fit_marked_hawkes_mle(
        [float(index) for index in range(60)],
        [index % 2 for index in range(60)],
        horizon=65.0,
        decay_grid=(0.1, 0.5),
        max_iterations=120,
    )
    engine.register_hawkes_model("BTCUSDT", fit)
    first = engine.observe_marked_event("btcusdt", 100.0, 1)
    second = engine.observe_marked_event("BTCUSDT", 101.0, 0)
    assert first.intensities == pytest.approx(fit.baseline_intensities)
    assert second.timestamp == 101.0
    with pytest.raises(KeyError):
        engine.observe_marked_event("ETHUSDT", 102.0, 0)


@pytest.mark.asyncio
async def test_drift_scheduler_retries_after_fail_closed_provider_error(tmp_path):
    registry = StatisticalModelRegistry(tmp_path / "models.sqlite3")
    runtime = StatisticalGovernanceRuntime(registry)
    engine = AStatEngine()
    observed = asyncio.Event()
    attempts = 0
    provider_threads = []
    event_loop_thread = threading.get_ident()

    def batch_provider():
        nonlocal attempts
        provider_threads.append(threading.get_ident())
        attempts += 1
        if attempts == 1:
            raise RuntimeError("current drift window is stale")
        observed.set()
        values = [float(index) for index in range(100)]
        return {"volatility": values}, {"volatility": values}

    task = await engine.start_drift_monitor(
        runtime, batch_provider, interval_seconds=1.0
    )
    await asyncio.wait_for(observed.wait(), timeout=3.0)
    await engine.stop_drift_monitor()
    assert task.done()
    assert attempts >= 2
    assert provider_threads
    assert all(thread_id != event_loop_thread for thread_id in provider_threads)
    assert runtime.last_drift is not None
    assert runtime.last_drift.status == "NORMAL"
    assert engine._drift_monitor_task is None
    assert engine._drift_monitor_scheduler is None
