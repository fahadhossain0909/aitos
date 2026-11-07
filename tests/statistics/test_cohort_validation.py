import pytest

from aitos.statistics.validation import regime_symbol_validation


def test_regime_symbol_validation_reports_under_sampled_cohorts():
    report = regime_symbol_validation(
        [0.8, 0.2, 0.7, 0.3],
        [1, 0, 1, 0],
        symbols=["BTCUSDT", "BTCUSDT", "ETHUSDT", "ETHUSDT"],
        regimes=["trend", "trend", "range", "range"],
        minimum_observations=3,
    )
    assert len(report) == 4
    assert all(item.observations == 2 for item in report)
    assert not any(item.sufficiently_sampled for item in report)
    by_cohort = {(item.cohort_type, item.cohort): item for item in report}
    assert by_cohort[("symbol", "BTCUSDT")].brier_score_value == pytest.approx(0.04)
    assert by_cohort[("symbol", "ETHUSDT")].brier_score_value == pytest.approx(0.09)
    assert by_cohort[("regime", "trend")].brier_score_value == pytest.approx(0.04)
    assert by_cohort[("regime", "range")].brier_score_value == pytest.approx(0.09)


def test_regime_symbol_validation_rejects_misaligned_or_invalid_inputs():
    with pytest.raises(ValueError):
        regime_symbol_validation([0.8], [1, 0], symbols=["BTCUSDT"], regimes=["trend"])
    with pytest.raises(ValueError):
        regime_symbol_validation([0.8], [2], symbols=["BTCUSDT"], regimes=["trend"])
