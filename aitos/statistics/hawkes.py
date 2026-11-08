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
    converged: bool = True
    gradient_norm: float = 0.0


def _spectral_radius_2x2(matrix):
    a, b = matrix[0]
    c, d = matrix[1]
    trace = a + d
    determinant = a * d - b * c
    discriminant = max(0.0, trace * trace - 4.0 * determinant)
    return max(
        abs((trace + math.sqrt(discriminant)) / 2.0),
        abs((trace - math.sqrt(discriminant)) / 2.0),
    )


def _project_stationary(matrix, limit: float = 0.949):
    radius = _spectral_radius_2x2(matrix)
    if radius >= limit:
        scale = limit / max(radius, 1e-15)
        return [[value * scale for value in row] for row in matrix]
    return matrix


def fit_marked_hawkes_mle(
    event_times: Iterable[float],
    marks: Iterable[int],
    *,
    horizon: float,
    decay_grid: tuple[float, ...] = (0.05, 0.1, 0.2, 0.5, 1.0, 2.0),
    max_iterations: int = 300,
) -> HawkesMLEFit:
    """Fit a two-mark exponential Hawkes model by constrained profile MLE.

    For each decay candidate, projected-gradient maximization uses an
    objective-monotone backtracking line search. The branching matrix spectral
    radius is constrained below 0.95. Event times use one consistent clock.
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
    if any(mark not in (0, 1) for mark in labels):
        raise ValueError("marks must be 0 or 1")
    if not math.isfinite(horizon) or horizon <= times[-1]:
        raise ValueError("horizon must be after the final event")
    if not decay_grid or any(not math.isfinite(x) or x <= 0 for x in decay_grid):
        raise ValueError("decay grid must contain positive finite values")
    if max_iterations <= 0:
        raise ValueError("max_iterations must be positive")

    counts = [labels.count(mark) for mark in range(2)]
    decay_candidates = set(float(value) for value in decay_grid)
    for _ in range(2):
        ordered_decays = sorted(decay_candidates)
        decay_candidates.update(
            math.sqrt(left * right)
            for left, right in zip(ordered_decays, ordered_decays[1:])
        )
    best = None
    for decay in sorted(decay_candidates):
        histories = []
        state = [0.0, 0.0]
        for index, event_time in enumerate(times):
            if index:
                factor = math.exp(-decay * (event_time - times[index - 1]))
                state = [value * factor for value in state]
            histories.append(tuple(state))
            state[labels[index]] += decay
        tails = [
            sum(
                1.0 - math.exp(-decay * (horizon - event_time))
                for event_time, mark in zip(times, labels)
                if mark == source
            )
            for source in range(2)
        ]
        event_indices = [
            [i for i, mark in enumerate(labels) if mark == target]
            for target in range(2)
        ]

        def objective(mu, excitation):
            value = -sum(mu) * horizon - sum(
                excitation[target][source] * tails[source]
                for target in range(2)
                for source in range(2)
            )
            for target in range(2):
                for index in event_indices[target]:
                    intensity = mu[target] + sum(
                        excitation[target][source] * histories[index][source]
                        for source in range(2)
                    )
                    if intensity <= 0 or not math.isfinite(intensity):
                        return -math.inf
                    value += math.log(intensity)
            return value

        mu = [max(counts[k] / horizon * 0.8, 1e-8) for k in range(2)]
        excitation = [[0.02, 0.01], [0.01, 0.02]]
        excitation = _project_stationary(excitation)
        current = objective(mu, excitation)
        step = 0.1
        converged = False
        gradient_norm = math.inf
        iterations = 0
        for iterations in range(1, max_iterations + 1):
            grad_mu = [-horizon for _ in range(2)]
            grad_a = [[-tails[source] for source in range(2)] for _ in range(2)]
            for target in range(2):
                for index in event_indices[target]:
                    intensity = max(
                        mu[target]
                        + sum(
                            excitation[target][source] * histories[index][source]
                            for source in range(2)
                        ),
                        1e-12,
                    )
                    grad_mu[target] += 1.0 / intensity
                    for source in range(2):
                        grad_a[target][source] += histories[index][source] / intensity
            # Normalize by event count to keep a scale-stable line search.
            # Apply the KKT projection at non-negative parameter boundaries:
            # a negative gradient at a zero coefficient points outside the
            # feasible set and must not prevent convergence at the MLE boundary.
            scale = float(len(times))
            projected_mu = [
                0.0 if mu[k] <= 1.0001e-9 and grad_mu[k] < 0.0 else grad_mu[k]
                for k in range(2)
            ]
            projected_a = [
                [
                    0.0
                    if excitation[target][source] <= 1e-12
                    and grad_a[target][source] < 0.0
                    else grad_a[target][source]
                    for source in range(2)
                ]
                for target in range(2)
            ]
            gradient_norm = math.sqrt(
                sum((value / scale) ** 2 for value in projected_mu)
                + sum((value / scale) ** 2 for row in projected_a for value in row)
            )
            if gradient_norm < 1e-5:
                converged = True
                break
            accepted = False
            trial_step = step
            for _ in range(30):
                candidate_mu = [
                    max(1e-9, mu[k] + trial_step * grad_mu[k] / scale) for k in range(2)
                ]
                candidate_a = [
                    [
                        max(0.0, excitation[k][m] + trial_step * grad_a[k][m] / scale)
                        for m in range(2)
                    ]
                    for k in range(2)
                ]
                candidate_a = _project_stationary(candidate_a)
                candidate = objective(candidate_mu, candidate_a)
                if math.isfinite(candidate) and candidate > current + 1e-12:
                    improvement = candidate - current
                    mu, excitation, current = candidate_mu, candidate_a, candidate
                    step = min(trial_step * 1.25, 2.0)
                    accepted = True
                    # Re-check the projected gradient on the next
                    # iteration; tiny objective movement alone is not proof
                    # of convergence near a constrained optimum.
                    break
                trial_step *= 0.5
            if not accepted:
                # Backtracking can stall at machine precision. Accept this as
                # convergence only when the feasible (KKT) gradient is small.
                converged = gradient_norm < 1e-5
                break
            if converged:
                break
        if best is None or current > best[0]:
            best = (
                current,
                decay,
                mu,
                excitation,
                iterations,
                converged,
                gradient_norm,
            )
    ll, decay, mu, excitation, iterations, converged, gradient_norm = best
    return HawkesMLEFit(
        tuple(mu),
        tuple(tuple(row) for row in excitation),
        decay,
        _spectral_radius_2x2(excitation),
        ll,
        iterations,
        len(times),
        horizon,
        converged,
        gradient_norm,
    )


@dataclass(frozen=True)
class HawkesIntensitySnapshot:
    timestamp: float
    intensities: tuple[float, float]
    excitation_state: tuple[float, float]


class MarkedHawkesOnlineFilter:
    """Causal two-mark event-time filter initialized from a fitted Hawkes MLE.

    mark 0 and mark 1 are application-defined event classes (for example,
    aggressive sell and buy). Intensities are evaluated immediately before
    processing the event at timestamp; the event then excites future events.
    """

    def __init__(self, fit: HawkesMLEFit) -> None:
        """Initialize only from a numerically valid, converged stationary fit."""
        if len(fit.baseline_intensities) != 2 or len(fit.excitation_matrix) != 2:
            raise ValueError("online filter requires a two-mark Hawkes fit")
        if any(len(row) != 2 for row in fit.excitation_matrix):
            raise ValueError("Hawkes excitation matrix must be 2x2")
        numeric = (
            *fit.baseline_intensities,
            *(value for row in fit.excitation_matrix for value in row),
            fit.decay,
            fit.branching_ratio,
            fit.log_likelihood,
            fit.gradient_norm,
            fit.horizon,
        )
        if any(not math.isfinite(float(value)) for value in numeric):
            raise ValueError("Hawkes fit parameters must be finite")
        if any(value <= 0 for value in fit.baseline_intensities):
            raise ValueError("Hawkes baseline intensities must be positive")
        if any(value < 0 for row in fit.excitation_matrix for value in row):
            raise ValueError("Hawkes excitation coefficients must be non-negative")
        if fit.decay <= 0 or fit.horizon <= 0 or fit.events < 20 or fit.iterations <= 0:
            raise ValueError("Hawkes fit metadata is invalid")
        if not fit.converged:
            raise ValueError("online filter refuses an unconverged Hawkes fit")
        actual_radius = _spectral_radius_2x2(fit.excitation_matrix)
        if not 0.0 <= fit.branching_ratio < 0.95 or actual_radius >= 0.95:
            raise ValueError("online filter refuses a non-stationary Hawkes fit")
        if not math.isclose(
            fit.branching_ratio, actual_radius, rel_tol=1e-5, abs_tol=1e-8
        ):
            raise ValueError("Hawkes branching ratio does not match excitation matrix")
        self.fit = fit
        self._state = [0.0, 0.0]
        self._last_timestamp: float | None = None

    def observe(self, timestamp: float, mark: int) -> HawkesIntensitySnapshot:
        """Return pre-event intensities and then apply the observed event."""
        timestamp = float(timestamp)
        if not math.isfinite(timestamp) or timestamp < 0 or mark not in (0, 1):
            raise ValueError("timestamp must be finite/non-negative and mark 0 or 1")
        if self._last_timestamp is not None and timestamp <= self._last_timestamp:
            raise ValueError("online Hawkes events must be strictly chronological")
        if self._last_timestamp is not None:
            decay = math.exp(-self.fit.decay * (timestamp - self._last_timestamp))
            self._state = [value * decay for value in self._state]
        intensities = tuple(
            self.fit.baseline_intensities[target]
            + sum(
                self.fit.excitation_matrix[target][source] * self._state[source]
                for source in range(2)
            )
            for target in range(2)
        )
        snapshot = HawkesIntensitySnapshot(timestamp, intensities, tuple(self._state))
        self._state[mark] += self.fit.decay
        self._last_timestamp = timestamp
        return snapshot

    @property
    def last_timestamp(self) -> float | None:
        return self._last_timestamp
