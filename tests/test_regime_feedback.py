import pytest

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


def test_champion_comparison_filters_non_finite_and_malformed_rewards():
    champion = [0.0] * 30 + [float("nan"), float("inf"), "invalid"]
    candidate = [1.0] * 30 + [float("-inf"), "not-a-number"]

    result = compare_champion_candidate(champion, candidate)

    assert result.accepted is True
    assert result.candidate_mean == 1.0
    assert result.champion_mean == 0.0


@pytest.mark.parametrize(
    ("min_improvement", "z_score"),
    [(float("nan"), 1.96), (0.05, float("inf")), (0.05, 0.0)],
)
def test_champion_comparison_rejects_non_finite_or_invalid_thresholds(
    min_improvement, z_score
):
    with pytest.raises(ValueError, match="configuration"):
        compare_champion_candidate(
            [0.0] * 30,
            [1.0] * 30,
            min_improvement=min_improvement,
            z_score=z_score,
        )
