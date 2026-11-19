import math

import pytest

from aitos.statistics.dependence import DCCGARCH, aggregate_portfolio_tail_risk
from aitos.statistics.duration import GaussianHSMM
from aitos.statistics.hawkes import (
    MarkedHawkesOnlineFilter,
    fit_marked_hawkes_mle,
)
from aitos.statistics.replay import BookSnapshot, replay_historical_order
from aitos.statistics.validation import multiclass_calibration_report


def test_hsmm_estimates_duration_distribution_and_normalized_posterior():
    values = []
    for block in range(12):
        state = 0.001 if block % 2 == 0 else -0.001
        values.extend([state + 0.0001 * math.sin(i) for i in range(8)])
    result = GaussianHSMM(states=2, max_duration=24).fit_filter(values)
    assert sum(result.probabilities) == pytest.approx(1.0)
    assert len(result.expected_durations) == 2
    assert all(value > 0 for value in result.expected_durations)
    assert all(sum(row) == pytest.approx(1.0) for row in result.duration_probabilities)


def test_hsmm_rejects_short_and_nonfinite_series():
    with pytest.raises(ValueError):
        GaussianHSMM().fit_filter([0.1] * 10)
    with pytest.raises(ValueError):
        GaussianHSMM().fit_filter([0.1] * 40 + [float("nan")])


def test_marked_continuous_time_hawkes_mle_is_stationary():
    times = [float(i) + 0.1 * math.sin(i) for i in range(60)]
    marks = [i % 2 for i in range(60)]
    result = fit_marked_hawkes_mle(
        times, marks, horizon=65.0, decay_grid=(0.1, 0.5, 1.0), max_iterations=100
    )
    assert result.events == 60
    assert math.isfinite(result.log_likelihood)
    assert result.branching_ratio < 0.95
    assert all(value > 0 for value in result.baseline_intensities)
    assert len(result.excitation_matrix) == 2


def test_hawkes_rejects_unsorted_or_unaligned_events():
    with pytest.raises(ValueError):
        fit_marked_hawkes_mle([1.0, 0.5], [0, 1], horizon=2.0)
    with pytest.raises(ValueError):
        fit_marked_hawkes_mle([1.0] * 20, [0] * 19, horizon=2.0)


def test_dcc_garch_and_gaussian_copula_are_valid_correlations():
    first = [0.001 * math.sin(i * 0.17) + 0.0002 * math.cos(i) for i in range(120)]
    second = [0.7 * first[i] + 0.0003 * math.sin(i * 0.31) for i in range(120)]
    result = DCCGARCH().fit_forecast({"BTCUSDT": first, "ETHUSDT": second})
    assert result.observations == 120
    assert result.persistence < 0.995
    assert result.student_t_copula is not None
    assert result.correlation_matrix[0][1] == pytest.approx(
        result.correlation_matrix[1][0]
    )
    assert -1.0 <= result.gaussian_copula_correlation[0][1] <= 1.0


def test_portfolio_tail_risk_uses_aligned_weighted_returns():
    returns = {
        "BTCUSDT": [0.01, -0.02, 0.005, -0.03] * 10,
        "ETHUSDT": [0.005, -0.01, 0.002, -0.02] * 10,
    }
    result = aggregate_portfolio_tail_risk(
        returns, {"BTCUSDT": 0.6, "ETHUSDT": 0.4}, confidence=0.95
    )
    assert result.observations == 40
    assert result.expected_shortfall >= result.value_at_risk


def test_l1_replay_models_latency_partial_fills_and_all_cost_components():
    snapshots = [
        BookSnapshot(1000, 99.0, 101.0, 0.5, 0.4),
        BookSnapshot(1100, 100.0, 102.0, 0.7, 0.6),
        BookSnapshot(1200, 101.0, 103.0, 1.0, 1.0),
    ]
    result = replay_historical_order(
        snapshots,
        side="BUY",
        quantity=1.0,
        submitted_at_ms=1000,
        latency_ms=100,
        taker_fee_bps=5.0,
        slippage_bps=2.0,
        funding_bps=1.0,
        borrow_bps=0.5,
        holding_hours=4.0,
    )
    assert result.complete
    assert result.filled_quantity == pytest.approx(1.0)
    assert len(result.fills) == 2
    assert result.average_fill_price > 102.0
    assert result.total_cost == pytest.approx(
        result.fees
        + result.spread_cost
        + result.slippage_cost
        + result.funding_cost
        + result.borrow_cost
    )


def test_l1_replay_preserves_unfilled_quantity_when_book_liquidity_is_missing():
    result = replay_historical_order(
        [BookSnapshot(1000, 99.0, 101.0, 0.0, 0.25)],
        side="BUY",
        quantity=1.0,
        submitted_at_ms=1000,
        latency_ms=0,
        taker_fee_bps=0.0,
    )
    assert result.filled_quantity == pytest.approx(0.25)
    assert result.remaining_quantity == pytest.approx(0.75)
    assert not result.complete


def test_hsmm_fit_filter_separation_does_not_refit_on_oos_observations():
    training = []
    for block in range(20):
        center = (0.001, -0.001, 0.002)[block % 3]
        training.extend(center + 0.0001 * math.sin(i * 0.7) for i in range(12))
    estimator = GaussianHSMM(states=3, max_duration=32)
    fitted = estimator.fit(training)
    holdout_a = [0.001 + 0.0002 * math.sin(i) for i in range(50)]
    holdout_b = [-0.004 + 0.0001 * math.cos(i) for i in range(50)]
    result_a = estimator.filter(holdout_a, fitted)
    result_b = estimator.filter(holdout_b, fitted)
    assert fitted.training_observations == len(training)
    assert result_a.state_means == result_b.state_means == fitted.state_means
    assert result_a.state_sigmas == result_b.state_sigmas == fitted.state_sigmas
    assert sum(result_a.probabilities) == pytest.approx(1.0)
    assert sum(result_b.probabilities) == pytest.approx(1.0)
    assert result_a.probabilities != result_b.probabilities


def test_hawkes_online_filter_is_causal_and_requires_monotonic_events():
    fit = fit_marked_hawkes_mle(
        [float(i) for i in range(60)],
        [i % 2 for i in range(60)],
        horizon=65.0,
        decay_grid=(0.1, 0.5),
        max_iterations=120,
    )
    online = MarkedHawkesOnlineFilter(fit)
    first = online.observe(100.0, 1)
    second = online.observe(101.0, 0)
    assert first.intensities == pytest.approx(fit.baseline_intensities)
    assert (
        max(second.intensities[i] - fit.baseline_intensities[i] for i in range(2)) > 0.0
    )
    assert online.last_timestamp == 101.0
    with pytest.raises(ValueError):
        online.observe(101.0, 1)


def test_hsmm_causal_oos_posterior_history_supports_multiclass_calibration():
    training = []
    for state, center in enumerate((-0.01, 0.0, 0.01)):
        training.extend(
            center + 0.0002 * math.sin(index * 0.31) for index in range(100)
        )
    holdout = []
    labels = []
    for index in range(90):
        state = (index // 10) % 3
        center = (-0.01, 0.0, 0.01)[state]
        holdout.append(center + 0.0002 * math.cos(index * 0.27))
        labels.append(state)
    estimator = GaussianHSMM(states=3, max_duration=40)
    parameters = estimator.fit(training)
    filtered = estimator.filter(holdout, parameters)
    report = multiclass_calibration_report(filtered.posterior_history, labels)
    assert report.observations == len(holdout)
    assert report.classes == 3
    assert 0.0 <= report.brier_score <= 2.0
    assert report.log_loss >= 0.0
    assert 0.0 <= report.expected_calibration_error <= 1.0
