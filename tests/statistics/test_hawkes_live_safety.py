"""Fail-closed validation for live marked-Hawkes event integration."""

from dataclasses import replace

import pytest

from aitos.statistics.engine import AStatEngine
from aitos.statistics.hawkes import (
    HawkesMLEFit,
    MarkedHawkesOnlineFilter,
)


def fitted_hawkes(**overrides):
    fit = HawkesMLEFit(
        baseline_intensities=(0.4, 0.5),
        excitation_matrix=((0.12, 0.0), (0.0, 0.10)),
        decay=0.7,
        branching_ratio=0.12,
        log_likelihood=-120.0,
        iterations=42,
        events=240,
        horizon=120.0,
        converged=True,
        gradient_norm=1e-9,
    )
    return replace(fit, **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"converged": False},
        {"baseline_intensities": (0.4, float("nan"))},
        {"baseline_intensities": (0.4, 0.0)},
        {"excitation_matrix": ((0.12, -0.03), (0.02, 0.10))},
        {"branching_ratio": 0.99},
        {"branching_ratio": 0.2},
        {"decay": 0.0},
        {"events": 0},
    ],
)
def test_online_filter_rejects_invalid_or_unconverged_fit(overrides):
    with pytest.raises(ValueError):
        MarkedHawkesOnlineFilter(fitted_hawkes(**overrides))


def test_engine_does_not_replace_healthy_filter_with_invalid_fit():
    engine = AStatEngine()
    engine.register_hawkes_model("btcusdt", fitted_hawkes())
    first = engine.observe_marked_event("BTCUSDT", 1.0, 0)

    with pytest.raises(ValueError, match="unconverged"):
        engine.register_hawkes_model("BTCUSDT", fitted_hawkes(converged=False))

    second = engine.observe_marked_event("BTCUSDT", 2.0, 1)
    assert first.timestamp == 1.0
    assert second.timestamp == 2.0


def test_engine_hawkes_updates_are_symbol_scoped_and_chronological():
    engine = AStatEngine()
    engine.register_hawkes_model("BTCUSDT", fitted_hawkes())

    with pytest.raises(KeyError):
        engine.observe_marked_event("ETHUSDT", 1.0, 0)

    engine.observe_marked_event("BTCUSDT", 2.0, 0)
    with pytest.raises(ValueError, match="strictly chronological"):
        engine.observe_marked_event("BTCUSDT", 2.0, 1)
