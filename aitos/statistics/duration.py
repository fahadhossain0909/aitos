"""Duration-aware hidden semi-Markov regime inference.

This module estimates Gaussian emissions and empirical state-duration
distributions, then performs explicit-duration filtering. It is a research
estimator: use chronological out-of-sample calibration before deployment.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Iterable


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


class GaussianHSMM:
    """Gaussian explicit-duration HSMM with empirical duration smoothing."""

    def __init__(self, states: int = 3, max_duration: int = 64, smoothing: float = 1.0):
        if states not in (2, 3) or max_duration < 2 or smoothing <= 0:
            raise ValueError("invalid HSMM configuration")
        self.states = states
        self.max_duration = max_duration
        self.smoothing = float(smoothing)

    def fit_filter(self, observations: Iterable[float]) -> HSMMResult:
        xs = tuple(float(x) for x in observations)
        if len(xs) < max(30, self.states * 8) or any(not math.isfinite(x) for x in xs):
            raise ValueError("HSMM requires enough finite chronological observations")
        ordered = sorted(xs)
        centers = [ordered[round((len(xs) - 1) * i / (self.states - 1))] for i in range(self.states)]
        for _ in range(40):
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
        order = sorted(range(self.states), key=lambda k: centers[k])
        remap = {old: new for new, old in enumerate(order)}
        labels = [remap[label] for label in labels]
        groups = [[] for _ in range(self.states)]
        for value, label in zip(xs, labels):
            groups[label].append(value)
        means = tuple(mean(group) if group else 0.0 for group in groups)
        global_sigma = max(pstdev(xs), 1e-8)
        sigmas = tuple(max(pstdev(group), global_sigma * 0.05, 1e-8) if group else global_sigma for group in groups)

        transitions = [[self.smoothing for _ in range(self.states)] for _ in range(self.states)]
        durations = [[self.smoothing for _ in range(self.max_duration + 1)] for _ in range(self.states)]
        runs = []
        start = 0
        for i in range(1, len(labels) + 1):
            if i == len(labels) or labels[i] != labels[start]:
                runs.append((labels[start], i - start))
                if i < len(labels):
                    transitions[labels[start]][labels[i]] += 1
                start = i
        for state, duration_value in runs:
            durations[state][min(duration_value, self.max_duration)] += 1
        for i in range(self.states):
            transitions[i][i] = 0.0
            total = sum(transitions[i])
            transitions[i] = [v / total for v in transitions[i]]
            total_duration = sum(durations[i][1:])
            durations[i][0] = 0.0
            durations[i] = [0.0] + [v / total_duration for v in durations[i][1:]]
        expected = tuple(
            sum(d * p for d, p in enumerate(row)) for row in durations
        )

        # Explicit-duration forward recursion. alpha[t][s] is the likelihood
        # of observations through t ending in a completed segment of state s.
        n = len(xs)
        log_emission = [[0.0] * (n + 1) for _ in range(self.states)]
        for state in range(self.states):
            for t, value in enumerate(xs, 1):
                z = (value - means[state]) / sigmas[state]
                log_emission[state][t] = log_emission[state][t - 1] - 0.5 * z * z - math.log(sigmas[state]) - 0.5 * math.log(2 * math.pi)
        alpha = [[0.0] * self.states for _ in range(n + 1)]
        for end in range(1, n + 1):
            for state in range(self.states):
                terms = []
                for duration_value in range(1, min(self.max_duration, end) + 1):
                    start_index = end - duration_value
                    probability = durations[state][duration_value]
                    if probability <= 0:
                        continue
                    segment_log = log_emission[state][end] - log_emission[state][start_index]
                    segment_weight = probability * math.exp(max(-700.0, min(50.0, segment_log)))
                    if start_index == 0:
                        prior = 1.0 / self.states
                    else:
                        prior = sum(
                            alpha[start_index][previous] * transitions[previous][state]
                            for previous in range(self.states) if previous != state
                        )
                    terms.append(segment_weight * prior)
                alpha[end][state] = sum(terms)
        posterior = alpha[n]
        total = sum(posterior)
        if not math.isfinite(total) or total <= 0:
            raise ArithmeticError("HSMM forward posterior collapsed")
        probs = tuple(value / total for value in posterior)
        return HSMMResult(
            probabilities=probs,
            most_likely_state=max(range(self.states), key=probs.__getitem__),
            state_means=means,
            state_sigmas=sigmas,
            expected_durations=expected,
            duration_probabilities=tuple(tuple(row) for row in durations),
            transition_matrix=tuple(tuple(row) for row in transitions),
            observations=n,
        )
