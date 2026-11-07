"""Deterministic synthetic-market acceptance tests for Statistical Intelligence.

These tests do not claim profitability. They verify numerical behavior, regime
sensitivity, dependence, event clustering, cost accounting and chronological
validation on seeded synthetic data before paper-trading evaluation.
"""

from __future__ import annotations

import math
import random

import pytest

from aitos.statistics.dependence import DCCGARCH, aggregate_portfolio_tail_risk
from aitos.statistics.duration import GaussianHSMM
from aitos.statistics.hawkes import fit_marked_hawkes_mle
from aitos.statistics.replay import BookSnapshot, replay_historical_order
from aitos.statistics.validation import (
    brier_score,
    combinatorial_purged_folds,
    expected_calibration_error,
    paired_block_bootstrap_difference,
    probability_of_backtest_overfitting,
    purged_walk_forward_folds,
)


def synthetic_market(seed: int = 20261107, observations: int = 960):
    """Generate three volatility regimes and a correlated second asset."""
    rng = random.Random(seed)
    primary = []
    secondary = []
    labels = []
    for i in range(observations):
        regime = min(2, i // (observations // 3))
        sigma = (0.0007, 0.0032, 0.0011)[regime]
        drift = (0.00004, -0.00002, 0.00003)[regime]
        common = rng.gauss(drift, sigma)
        primary.append(common)
        secondary.append(0.72 * common + rng.gauss(0.0, sigma * 0.55))
        labels.append(regime)
    return primary, secondary, labels


def clustered_marked_events(seed: int = 20261107, count: int = 240):
    """Generate a deterministic self-exciting-like event stream."""
    rng = random.Random(seed)
    times = []
    marks = []
    clock = 0.0
    excitation = [0.0, 0.0]
    for _ in range(count):
        intensity = 0.7 + 1.8 * max(excitation)
        clock += rng.expovariate(intensity)
        mark = 0 if rng.random() < 0.53 else 1
        times.append(clock)
        marks.append(mark)
        elapsed = times[-1] - (times[-2] if len(times) > 1 else 0.0)
        excitation = [value * math.exp(-0.35 * elapsed) for value in excitation]
        excitation[mark] += 0.35
    return times, marks, clock + 2.0


def test_seeded_regime_simulation_produces_stable_hsmm_posterior():
    primary, _, labels = synthetic_market()
    fit = GaussianHSMM(states=3, max_duration=80).fit_filter(primary)
    assert len(fit.probabilities) == 3
    assert all(math.isfinite(p) and 0.0 <= p <= 1.0 for p in fit.probabilities)
    assert sum(fit.probabilities) == pytest.approx(1.0, abs=1e-8)
    assert all(math.isfinite(x) and x > 0 for x in fit.expected_durations)
    assert len(set(labels)) == 3


def test_clustered_event_simulation_hawkes_fit_is_stationary_and_finite():
    times, marks, horizon = clustered_marked_events()
    fit = fit_marked_hawkes_mle(
        times,
        marks,
        horizon=horizon,
        decay_grid=(0.15, 0.35, 0.7),
        max_iterations=250,
    )
    assert fit.events == len(times)
    assert fit.horizon == pytest.approx(horizon)
    assert math.isfinite(fit.log_likelihood)
    assert fit.iterations > 0
    assert math.isfinite(fit.gradient_norm)
    assert isinstance(fit.converged, bool)
    assert 0.0 <= fit.branching_ratio < 0.95
    assert all(math.isfinite(x) and x > 0 for x in fit.baseline_intensities)
    assert all(
        math.isfinite(x) and x >= 0 for row in fit.excitation_matrix for x in row
    )


def test_correlated_regime_market_dcc_and_portfolio_tail_risk():
    first, second, _ = synthetic_market()
    result = DCCGARCH().fit_forecast({"BTCUSDT": first, "ETHUSDT": second})
    assert result.observations == len(first)
    assert result.persistence < 0.995
    rho = result.correlation_matrix[0][1]
    assert math.isfinite(rho) and 0.15 < rho < 0.99
    assert result.correlation_matrix[0][0] == pytest.approx(1.0, abs=1e-6)
    risk = aggregate_portfolio_tail_risk(
        {"BTCUSDT": first, "ETHUSDT": second},
        {"BTCUSDT": 0.6, "ETHUSDT": 0.4},
        confidence=0.975,
    )
    assert risk.observations == len(first)
    assert risk.expected_shortfall >= risk.value_at_risk >= 0.0
    assert math.isfinite(risk.expected_shortfall)


def test_synthetic_execution_replay_accounts_for_latency_partial_fills_and_costs():
    snapshots = [
        BookSnapshot(
            1_000 + i * 100,
            100.0 + i * 0.01,
            100.2 + i * 0.01,
            0.4,
            0.35,
        )
        for i in range(12)
    ]
    replay = replay_historical_order(
        snapshots,
        side="BUY",
        quantity=2.0,
        submitted_at_ms=1_000,
        latency_ms=200,
        taker_fee_bps=5.0,
        slippage_bps=1.5,
        funding_bps=2.0,
        borrow_bps=0.5,
        holding_hours=12.0,
    )
    assert replay.complete
    assert replay.filled_quantity == pytest.approx(2.0)
    assert replay.remaining_quantity == pytest.approx(0.0)
    assert len(replay.fills) > 1
    assert replay.total_cost == pytest.approx(
        replay.fees
        + replay.spread_cost
        + replay.slippage_cost
        + replay.funding_cost
        + replay.borrow_cost
    )
    assert replay.average_fill_price is not None


def test_validation_metrics_and_resampling_on_synthetic_oos_predictions():
    rng = random.Random(73)
    outcomes = [1 if rng.random() < 0.52 else 0 for _ in range(400)]
    probabilities = [0.52 + rng.uniform(-0.08, 0.08) for _ in outcomes]
    assert 0.0 <= brier_score(probabilities, outcomes) <= 1.0
    assert 0.0 <= expected_calibration_error(probabilities, outcomes) <= 1.0

    folds = purged_walk_forward_folds(
        400, n_splits=4, test_size=50, label_horizon=5, embargo=3
    )
    assert len(folds) == 4
    for fold in folds:
        assert max(fold.train_indices) < min(fold.test_indices)
        assert min(fold.test_indices) - max(fold.train_indices) >= 5

    cpcv = combinatorial_purged_folds(
        400, n_groups=8, n_test_groups=2, purge=2, embargo=2
    )
    assert len(cpcv) == 28
    assert all(set(f.train_indices).isdisjoint(f.test_indices) for f in cpcv)

    incumbent = [rng.gauss(0.0001, 0.001) for _ in range(400)]
    candidate = [value + 0.00015 for value in incumbent]
    interval = paired_block_bootstrap_difference(
        candidate, incumbent, block_length=8, n_resamples=500, seed=73
    )
    assert interval.estimate == pytest.approx(0.00015)
    assert interval.lower <= interval.estimate <= interval.upper


def test_pbo_diagnostic_runs_on_synthetic_strategy_panel():
    rng = random.Random(91)
    panel = []
    for strategy in range(6):
        edge = 0.00012 if strategy == 0 else 0.00002 * (5 - strategy)
        panel.append([rng.gauss(edge, 0.001) for _ in range(800)])
    pbo = probability_of_backtest_overfitting(panel, n_slices=8)
    assert math.isfinite(pbo)
    assert 0.0 <= pbo <= 1.0
