from datetime import datetime, timedelta, timezone

from aitos.decision.calibration import CalibrationMetrics, ProbabilityCalibrator
from aitos.decision.dsl import parse_decision_spec
from aitos.decision.replay import (
    GovernanceThresholds,
    ReplayObservation,
    assess_challenger,
    replay_chronologically,
)
from aitos.decision.runtime import ADLRuntime
from aitos.decision.types import DecisionPolicy, MarketSnapshot, ModelResponse


class _Provider:
    def evaluate(self, snapshot, spec):
        return ModelResponse(
            probabilities={"LONG": 0.7, "SHORT": 0.1, "NO_TRADE": 0.2},
            model_id="replay",
            model_version="1",
        )


def test_replay_is_chronological_and_updates_calibration_after_decision():
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    spec = parse_decision_spec(
        "DECISION test:\nSTATE:\n - x\nASK side: CHOICE [LONG, SHORT, NO_TRADE]"
    )
    snapshots = [
        MarketSnapshot(
            "BTCUSDT", now + timedelta(minutes=i), {"x": i}, snapshot_id=str(i)
        )
        for i in (2, 0, 1)
    ]
    rows = [
        (
            ReplayObservation(
                snapshot, "LONG", snapshot.timestamp + timedelta(seconds=30)
            )
            if snapshot.snapshot_id == "0"
            else ReplayObservation(
                snapshot, "LONG", snapshot.timestamp + timedelta(minutes=5)
            )
        )
        for snapshot in snapshots
    ]
    calibrator = ProbabilityCalibrator(minimum_samples=1)
    runtime = ADLRuntime(
        _Provider(),
        policy=DecisionPolicy(
            require_calibration=False, require_positive_expected_value=False
        ),
    )
    report = replay_chronologically(runtime, spec, rows, calibrator)
    assert report.observations == 3
    assert [result.snapshot_id for result in report.results] == ["0", "1", "2"]
    assert report.results[1].calibrated is True
    assert calibrator.metrics("LONG").samples == 3


def test_governance_blocks_insufficient_or_poor_challenger():
    result = assess_challenger(
        CalibrationMetrics(10, 0.4, 1.0, 0.3),
        abstention_rate=0.99,
        thresholds=GovernanceThresholds(minimum_samples=100),
    )
    assert result.eligible_for_shadow is False
    assert result.eligible_for_canary is False
    assert len(result.reasons) == 4


def test_governance_allows_canary_only_with_mature_good_oos_metrics():
    result = assess_challenger(
        CalibrationMetrics(500, 0.12, 0.3, 0.04), abstention_rate=0.2
    )
    assert result.eligible_for_shadow is True
    assert result.eligible_for_canary is True
