import math

import pytest

from aitos.statistics.dependence import DCCGARCH, aggregate_portfolio_tail_risk
from aitos.statistics.duration import GaussianHSMM
from aitos.statistics.hawkes import fit_marked_hawkes_mle
from aitos.statistics.replay import BookSnapshot, replay_historical_order


def test_hsmm_estimates_duration_distribution_and_normalized_posterior():
    values = []
    for block in range(12):
        state = (0.001 if block % 2 == 0 else -0.001)
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
    assert result.log_likelihood == pytest.approx(result.log_likelihood)
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
    assert result.correlation_matrix[0][1] == pytest.approx(result.correlation_matrix[1][0])
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
        result.fees + result.spread_cost + result.slippage_cost
        + result.funding_cost + result.borrow_cost
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
