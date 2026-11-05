from aitos.learning.regime_feedback import (
    RegimeRewardCalibrator,
    compare_champion_candidate,
    normalize_regime,
    outcome_feedback,
)


def test_reward_calibration_is_partitioned_by_regime_and_robust_to_outliers():
    calibrator = RegimeRewardCalibrator(min_samples=3)
    for reward in (0.2, 0.3, 0.1):
        calibrator.calibrate("trending", reward)
    trending = calibrator.calibrate("trending", 20.0)
    ranging = calibrator.calibrate("ranging", 20.0)

    assert trending < 1.0
    assert ranging == 5.0
    snapshot = calibrator.snapshot()
    restored = RegimeRewardCalibrator(min_samples=3)
    restored.restore(snapshot)
    assert set(restored.snapshot()) == {"trending", "ranging"}


def test_outcome_feedback_flags_explicit_premature_exit_and_missed_move():
    feedback = outcome_feedback(
        realized_r=0.4, mfe_r=2.0, exit_reason="adaptive_stagnation"
    )
    assert feedback.premature_exit is True
    assert feedback.missed_opportunity_r == 1.6


def test_champion_challenger_gate_requires_sample_floor_and_significance():
    small = compare_champion_candidate([1.0] * 10, [1.5] * 10)
    assert small.accepted is False
    assert small.reason == "insufficient samples"

    accepted = compare_champion_candidate([0.0] * 40, [1.0] * 40)
    assert accepted.accepted is True


def test_regime_partition_key_cannot_escape_checkpoint_directory():
    assert normalize_regime("../../outside/path") == "outside_path"


def test_reward_calibration_is_partitioned_by_strategy_within_regime():
    calibrator = RegimeRewardCalibrator(min_samples=3)
    for reward in (0.2, 0.3, 0.1):
        calibrator.calibrate("trending", reward, strategy_id="mean_reversion")

    clipped = calibrator.calibrate(
        "trending", 20.0, strategy_id="mean_reversion"
    )
    independent = calibrator.calibrate(
        "trending", 20.0, strategy_id="breakout"
    )

    assert clipped < 1.0
    assert independent == 5.0
    snapshot = calibrator.snapshot()
    assert "mean_reversion__trending" in snapshot
    assert "breakout__trending" in snapshot
