import hashlib

from aitos.statistics.governance import PromotionEvidence, PromotionPolicy
from aitos.statistics.governance_runtime import (
    DriftMonitorScheduler,
    StatisticalGovernanceRuntime,
)
from aitos.statistics.registry import StatisticalModelRegistry


def _evidence(version="2.0.0", approved=True):
    return PromotionEvidence(
        model_id="a-stat",
        model_version=version,
        observations=2000,
        shadow_days=45,
        brier=0.18,
        qlike=1.2,
        pbo=0.08,
        max_drawdown=0.05,
        drift_score=0.02,
        paired_oos_improvement_lower_bound=0.01,
        dsr_probability=0.99,
        mcs_included=True,
        reality_check_p_value=0.01,
        dm_p_value=0.01,
        multiple_testing_adjusted_p_value=0.02,
        operator_approved=approved,
    )


def _register(registry, version):
    digest = hashlib.sha256(version.encode()).hexdigest()
    registry.register_candidate(
        model_id="a-stat",
        model_version=version,
        artifact_uri=f"models/{version}.bin",
        artifact_sha256=digest,
        config_hash=digest,
        feature_schema_version="1",
    )


def test_live_drift_blocks_challenger_promotion_and_is_audited(tmp_path):
    registry = StatisticalModelRegistry(tmp_path / "registry.sqlite3")
    _register(registry, "2.0.0")
    runtime = StatisticalGovernanceRuntime(
        registry,
        policy=PromotionPolicy(min_observations=100, min_shadow_days=1),
    )
    reference = {"volatility": [float(i) for i in range(100)]}
    current = {"volatility": [float(i + 1000) for i in range(100)]}
    drift = runtime.observe_live_features(reference, current)
    assert drift.status in {"WARNING", "CRITICAL"}
    decision = runtime.evaluate_challenger(_evidence())
    assert not decision.eligible
    assert "live_feature_drift_blocks_promotion" in decision.reasons
    blocked = runtime.promote_challenger(_evidence(), operator_approved=True)
    assert not blocked.eligible
    assert registry.get_champion("a-stat") is None
    assert registry.promotion_history("a-stat")[-1]["action"] == "PROMOTION_BLOCKED"


def test_challenger_requires_explicit_runtime_operator_approval(tmp_path):
    registry = StatisticalModelRegistry(tmp_path / "registry.sqlite3")
    _register(registry, "2.0.0")
    runtime = StatisticalGovernanceRuntime(
        registry,
        policy=PromotionPolicy(min_observations=100, min_shadow_days=1),
    )
    reference = {"volatility": [float(i) for i in range(100)]}
    runtime.observe_live_features(reference, reference)
    blocked = runtime.promote_challenger(
        _evidence(approved=True), operator_approved=False
    )
    assert not blocked.eligible
    assert registry.get_champion("a-stat") is None
    promoted = runtime.promote_challenger(
        _evidence(approved=True), operator_approved=True
    )
    assert promoted.eligible
    assert registry.get_champion("a-stat").model_version == "2.0.0"


def test_drift_scheduler_respects_cadence_and_deduplicates_alerts(tmp_path):
    import asyncio
    from datetime import datetime, timedelta, timezone

    registry = StatisticalModelRegistry(tmp_path / "registry.sqlite3")
    runtime = StatisticalGovernanceRuntime(registry)
    alerts = []
    scheduler = DriftMonitorScheduler(
        runtime, interval_seconds=60, alert_callback=alerts.append
    )
    reference = {"volatility": [float(i) for i in range(100)]}
    shifted = {"volatility": [float(i + 1000) for i in range(100)]}
    start = datetime(2026, 11, 7, tzinfo=timezone.utc)

    first = asyncio.run(scheduler.run_once(reference, shifted, now=start))
    skipped = asyncio.run(
        scheduler.run_once(reference, shifted, now=start + timedelta(seconds=20))
    )
    second = asyncio.run(
        scheduler.run_once(reference, shifted, now=start + timedelta(seconds=61))
    )
    assert first.status in {"WARNING", "CRITICAL"}
    assert skipped is None
    assert second.status == first.status
    assert len(alerts) == 1


def test_engine_drift_shutdown_clears_state_after_provider_failure(tmp_path):
    import asyncio

    from aitos.statistics.engine import AStatEngine

    registry = StatisticalModelRegistry(tmp_path / "registry.sqlite3")
    runtime = StatisticalGovernanceRuntime(registry)
    engine = AStatEngine()

    async def broken_provider():
        raise RuntimeError("feature store unavailable")

    async def healthy_provider():
        reference = {"volatility": [float(i) for i in range(100)]}
        return reference, reference

    async def exercise():
        await engine.start_drift_monitor(runtime, broken_provider, interval_seconds=1)
        await asyncio.sleep(0.05)
        # Provider failures are fail-closed and retried; they must not kill the
        # managed worker or escape from cooperative shutdown.
        await engine.stop_drift_monitor()
        assert engine._drift_monitor_task is None
        assert engine._drift_monitor_scheduler is None

        # A recovered provider can start a fresh managed worker after shutdown.
        await engine.start_drift_monitor(runtime, healthy_provider, interval_seconds=1)
        await engine.stop_drift_monitor()
        assert engine._drift_monitor_task is None

    asyncio.run(exercise())



def test_provider_failure_marks_drift_unavailable_and_blocks_promotion(tmp_path):
    import asyncio
    import threading

    registry = StatisticalModelRegistry(tmp_path / "registry.sqlite3")
    _register(registry, "2.0.0")
    runtime = StatisticalGovernanceRuntime(
        registry,
        policy=PromotionPolicy(min_observations=100, min_shadow_days=1),
    )
    reference = {"volatility": [float(i) for i in range(100)]}
    runtime.observe_live_features(reference, reference)
    scheduler = DriftMonitorScheduler(runtime, interval_seconds=1)
    first_failure = threading.Event()
    attempts = 0

    def provider():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_failure.set()
            raise RuntimeError("temporary feature store outage")
        return reference, reference

    async def exercise():
        stop_event = asyncio.Event()
        task = asyncio.create_task(scheduler.run_forever(stop_event, provider))
        assert await asyncio.to_thread(first_failure.wait, 2.0)
        for _ in range(50):
            if runtime.last_drift and runtime.last_drift.status == "UNAVAILABLE":
                break
            await asyncio.sleep(0.01)
        assert runtime.last_drift is not None
        assert runtime.last_drift.status == "UNAVAILABLE"
        assert runtime.last_drift.promotion_blocked
        decision = runtime.evaluate_challenger(_evidence())
        assert not decision.eligible
        assert "live_drift_data_unavailable_blocks_promotion" in decision.reasons
        stop_event.set()
        await task

    asyncio.run(exercise())
