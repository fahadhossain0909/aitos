"""Leakage-aware Gaussian explicit-duration hidden semi-Markov inference.

Fit parameters only on a historical training window, then call filter on later
observations. The convenience fit_filter method remains for compatibility but
is in-sample and must not be used as an out-of-sample evaluation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Iterable


@dataclass(frozen=True)
class HSMMParameters:
    state_means: tuple[float, ...]
    state_sigmas: tuple[float, ...]
    expected_durations: tuple[float, ...]
    duration_probabilities: tuple[tuple[float, ...], ...]
    transition_matrix: tuple[tuple[float, ...], ...]
    training_observations: int
    max_duration: int


@dataclass(frozen=True)
class HSMMResult:
    probabilities: tuple[float, ...]
    most_likely_state: int
    state_means: tuple[float, ...]
    state_sigmas: tuple[float, ...]
    expected_durations: tuple[float, ...]
    duration_probabilities: tuple[tuple[float, ...], ...]
    transition_matrix: tuple[tuple[float, ...], ...]
    observations: int
    posterior_history: tuple[tuple[float, ...], ...] = ()


def _logsumexp(values: Iterable[float]) -> float:
    finite = [value for value in values if math.isfinite(value)]
    if not finite:
        return -math.inf
    maximum = max(finite)
    return maximum + math.log(sum(math.exp(value - maximum) for value in finite))


class GaussianHSMM:
    """Gaussian-emission HSMM with empirical durations and causal filtering."""

    def __init__(self, states: int = 3, max_duration: int = 64, smoothing: float = 1.0):
        if states not in (2, 3) or max_duration < 2 or smoothing <= 0:
            raise ValueError("invalid HSMM configuration")
        self.states = states
        self.max_duration = max_duration
        self.smoothing = float(smoothing)

    @staticmethod
    def _observations(values: Iterable[float], minimum: int) -> tuple[float, ...]:
        xs = tuple(float(value) for value in values)
        if len(xs) < minimum or any(not math.isfinite(value) for value in xs):
            raise ValueError("HSMM requires enough finite chronological observations")
        return xs

    def fit(self, observations: Iterable[float]) -> HSMMParameters:
        """Estimate emissions/transitions/durations using training data only."""
        xs = self._observations(observations, max(30, self.states * 8))
        ordered = sorted(xs)
        centers = [
            ordered[round((len(xs) - 1) * i / (self.states - 1))]
            for i in range(self.states)
        ]
        for _ in range(100):
            groups = [[] for _ in range(self.states)]
            labels = []
            for value in xs:
                state = min(range(self.states), key=lambda k: abs(value - centers[k]))
                labels.append(state)
                groups[state].append(value)
            updated = [
                mean(group) if group else centers[k] for k, group in enumerate(groups)
            ]
            if max(abs(a - b) for a, b in zip(updated, centers)) < 1e-12:
                break
            centers = updated
        if any(not group for group in groups):
            raise ValueError(
                "HSMM training data does not identify every configured state"
            )
        order = sorted(range(self.states), key=lambda k: centers[k])
        remap = {old: new for new, old in enumerate(order)}
        labels = [remap[label] for label in labels]
        groups = [[] for _ in range(self.states)]
        for value, label in zip(xs, labels):
            groups[label].append(value)
        means = tuple(mean(group) for group in groups)
        global_sigma = max(pstdev(xs), 1e-8)
        sigmas = tuple(
            max(pstdev(group), global_sigma * 0.05, 1e-8) for group in groups
        )

        transitions = [
            [self.smoothing for _ in range(self.states)] for _ in range(self.states)
        ]
        durations = [
            [self.smoothing for _ in range(self.max_duration + 1)]
            for _ in range(self.states)
        ]
        start = 0
        for i in range(1, len(labels) + 1):
            if i == len(labels) or labels[i] != labels[start]:
                state = labels[start]
                durations[state][min(i - start, self.max_duration)] += 1.0
                if i < len(labels):
                    transitions[state][labels[i]] += 1.0
                start = i
        for state in range(self.states):
            transitions[state][state] = 0.0
            total = sum(transitions[state])
            transitions[state] = [value / total for value in transitions[state]]
            durations[state][0] = 0.0
            total_duration = sum(durations[state][1:])
            durations[state] = [0.0] + [
                value / total_duration for value in durations[state][1:]
            ]
        duration_rows = tuple(tuple(row) for row in durations)
        expected = tuple(sum(d * p for d, p in enumerate(row)) for row in duration_rows)
        return HSMMParameters(
            means,
            sigmas,
            expected,
            duration_rows,
            tuple(tuple(row) for row in transitions),
            len(xs),
            self.max_duration,
        )

    def filter(
        self, observations: Iterable[float], parameters: HSMMParameters
    ) -> HSMMResult:
        """Causal forward filter; parameters must come from an earlier window."""
        xs = self._observations(observations, 1)
        if (
            len(parameters.state_means) != self.states
            or parameters.max_duration != self.max_duration
            or len(parameters.state_sigmas) != self.states
        ):
            raise ValueError("HSMM parameters do not match estimator configuration")
        n = len(xs)
        log_emission = [[0.0] * (n + 1) for _ in range(self.states)]
        for state in range(self.states):
            mu = parameters.state_means[state]
            sigma = parameters.state_sigmas[state]
            for t, value in enumerate(xs, 1):
                z = (value - mu) / sigma
                log_emission[state][t] = (
                    log_emission[state][t - 1]
                    - 0.5 * z * z
                    - math.log(sigma)
                    - 0.5 * math.log(2 * math.pi)
                )

        # alpha[t][s] is the log likelihood of a sequence ending in a
        # completed segment of state s at t (exact duration probability).
        alpha = [[-math.inf] * self.states for _ in range(n + 1)]
        filtered = (0.0,) * self.states
        posterior_history = []
        for end in range(1, n + 1):
            completed = []
            current_state_terms = [[] for _ in range(self.states)]
            for state in range(self.states):
                exact_terms = []
                for duration in range(1, min(self.max_duration, end) + 1):
                    start = end - duration
                    segment = log_emission[state][end] - log_emission[state][start]
                    if start == 0:
                        prior = -math.log(self.states)
                    else:
                        prior = _logsumexp(
                            alpha[start][previous]
                            + math.log(
                                max(
                                    parameters.transition_matrix[previous][state],
                                    1e-300,
                                )
                            )
                            for previous in range(self.states)
                            if previous != state
                        )
                    mass = parameters.duration_probabilities[state][duration]
                    if mass > 0:
                        exact_terms.append(math.log(mass) + segment + prior)
                    survival = sum(parameters.duration_probabilities[state][duration:])
                    if survival > 0:
                        current_state_terms[state].append(
                            math.log(survival) + segment + prior
                        )
                alpha[end][state] = _logsumexp(exact_terms)
                completed.append(alpha[end][state])
            normalizer = _logsumexp(
                value for row in current_state_terms for value in row
            )
            if not math.isfinite(normalizer):
                raise ArithmeticError("HSMM forward filter posterior collapsed")
            probabilities = tuple(
                math.exp(_logsumexp(terms) - normalizer) if terms else 0.0
                for terms in current_state_terms
            )
            total = sum(probabilities)
            filtered = tuple(value / total for value in probabilities)
            posterior_history.append(filtered)
        return HSMMResult(
            filtered,
            max(range(self.states), key=filtered.__getitem__),
            parameters.state_means,
            parameters.state_sigmas,
            parameters.expected_durations,
            parameters.duration_probabilities,
            parameters.transition_matrix,
            n,
            tuple(posterior_history),
        )

    def fit_filter(self, observations: Iterable[float]) -> HSMMResult:
        """Compatibility helper; fits and filters the same (in-sample) window."""
        xs = self._observations(observations, max(30, self.states * 8))
        return self.filter(xs, self.fit(xs))
