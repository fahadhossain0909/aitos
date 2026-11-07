"""Marked continuous-time exponential Hawkes maximum-likelihood estimator."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class HawkesMLEFit:
    baseline_intensities: tuple[float, ...]
    excitation_matrix: tuple[tuple[float, ...], ...]
    decay: float
    branching_ratio: float
    log_likelihood: float
    iterations: int
    events: int
    horizon: float


def _spectral_radius_2x2(matrix):
    a, b = matrix[0]
    c, d = matrix[1]
    trace = a + d
    determinant = a * d - b * c
    discriminant = max(0.0, trace * trace - 4.0 * determinant)
    return max(
        abs((trace + math.sqrt(discriminant)) / 2),
        abs((trace - math.sqrt(discriminant)) / 2),
    )


def fit_marked_hawkes_mle(
    event_times: Iterable[float],
    marks: Iterable[int],
    *,
    horizon: float,
    decay_grid: tuple[float, ...] = (0.05, 0.1, 0.2, 0.5, 1.0, 2.0),
    max_iterations: int = 300,
) -> HawkesMLEFit:
    """Fit a two-mark exponential Hawkes process by constrained profile MLE.

    Excitation coefficients are branching-matrix entries; spectral radius is
    constrained below 0.95 for stationarity. The decay parameter is profiled
    over an explicit grid and the remaining parameters use projected-gradient
    likelihood maximisation. Times are seconds in one consistent clock.
    """
    times = tuple(float(t) for t in event_times)
    labels = tuple(int(m) for m in marks)
    if len(times) < 20 or len(times) != len(labels):
        raise ValueError("at least 20 aligned marked events are required")
    if any(not math.isfinite(t) or t < 0 for t in times) or any(
        a >= b for a, b in zip(times, times[1:])
    ):
        raise ValueError(
            "event times must be finite, non-negative and strictly increasing"
        )
    if any(m not in (0, 1) for m in labels):
        raise ValueError("marks must be 0 or 1")
    if not math.isfinite(horizon) or horizon <= times[-1]:
        raise ValueError("horizon must be after the final event")
    if not decay_grid or any(not math.isfinite(x) or x <= 0 for x in decay_grid):
        raise ValueError("decay grid must contain positive finite values")
    counts = [labels.count(k) for k in range(2)]
    best = None
    for decay in decay_grid:
        history = [[0.0, 0.0] for _ in times]
        state = [0.0, 0.0]
        for i, t in enumerate(times):
            if i:
                factor = math.exp(-decay * (t - times[i - 1]))
                state = [state[m] * factor for m in range(2)]
            # Store pre-event excitation: an event cannot excite itself.
            history[i] = state[:]
            state[labels[i]] += decay
        tails = [
            sum(
                1.0 - math.exp(-decay * (horizon - t))
                for t, mark in zip(times, labels)
                if mark == m
            )
            for m in range(2)
        ]
        mu = [max(counts[k] / horizon * 0.5, 1e-8) for k in range(2)]
        excitation = [[0.01, 0.005], [0.005, 0.01]]
        rate = 0.02
        last_ll = -float("inf")
        iterations = 0
        for iterations in range(1, max_iterations + 1):
            gradients_mu = [0.0, 0.0]
            gradients_a = [[0.0, 0.0] for _ in range(2)]
            ll = -sum(mu) * horizon - sum(
                excitation[k][m] * tails[m] for k in range(2) for m in range(2)
            )
            for i, mark in enumerate(labels):
                intensity = mu[mark] + sum(
                    excitation[mark][m] * history[i][m] for m in range(2)
                )
                intensity = max(intensity, 1e-12)
                ll += math.log(intensity)
                gradients_mu[mark] += 1.0 / intensity
                for m in range(2):
                    gradients_a[mark][m] += history[i][m] / intensity
            for k in range(2):
                gradients_mu[k] -= horizon
                for m in range(2):
                    gradients_a[k][m] -= tails[m]
            if ll > last_ll + 1e-9:
                rate = min(rate * 1.05, 0.05)
            else:
                rate *= 0.5
            if rate < 1e-9:
                break
            candidate_mu = [
                max(1e-9, mu[k] + rate * gradients_mu[k] / len(times)) for k in range(2)
            ]
            candidate_a = [
                [
                    max(
                        0.0,
                        excitation[k][m] + rate * gradients_a[k][m] / len(times),
                    )
                    for m in range(2)
                ]
                for k in range(2)
            ]
            radius = _spectral_radius_2x2(candidate_a)
            if radius >= 0.95:
                scale = 0.949 / radius
                candidate_a = [
                [value * scale for value in row] for row in candidate_a
            ]
            mu, excitation = candidate_mu, candidate_a
            if abs(ll - last_ll) < 1e-10:
                break
            last_ll = ll
        # Re-evaluate likelihood at final parameters.
        ll = -sum(mu) * horizon - sum(
            excitation[k][m] * tails[m] for k in range(2) for m in range(2)
        )
        for i, mark in enumerate(labels):
            intensity = mu[mark] + sum(
                excitation[mark][m] * history[i][m] for m in range(2)
            )
            ll += math.log(max(intensity, 1e-12))
        if best is None or ll > best[0]:
            best = (ll, decay, mu, excitation, iterations)
    ll, decay, mu, excitation, iterations = best
    radius = _spectral_radius_2x2(excitation)
    return HawkesMLEFit(
        tuple(mu),
        tuple(tuple(row) for row in excitation),
        decay,
        radius,
        ll,
        iterations,
        len(times),
        horizon,
    )
