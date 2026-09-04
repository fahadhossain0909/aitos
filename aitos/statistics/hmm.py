"""Lightweight Gaussian HMM / Markov-switching regime estimator."""
from __future__ import annotations

import math
from collections.abc import Sequence

from .models import HMMState


def _normal_pdf(x: float, mean: float, sigma: float) -> float:
    sigma = max(sigma, 1e-8)
    z = (x - mean) / sigma
    return math.exp(-0.5 * z * z) / (sigma * math.sqrt(2.0 * math.pi))


class MarkovSwitchingModel:
    """Two/three-state Gaussian HMM using bounded EM-style iterations.

    State ordering is low-vol/negative, neutral, positive when three states are used.
    The implementation is dependency-light and deterministic for production services.
    """

    def __init__(self, states: int = 3, iterations: int = 8) -> None:
        if states not in (2, 3):
            raise ValueError("states must be 2 or 3")
        self.states = states
        self.iterations = iterations

    def fit_predict(self, returns: Sequence[float]) -> HMMState:
        xs = [float(x) for x in returns if math.isfinite(float(x))]
        if not xs:
            p = (1.0 / self.states,) * self.states
            t = tuple(tuple(1.0 / self.states for _ in p) for _ in p)
            return HMMState(p, 0, t, (0.0,) * self.states, (1.0,) * self.states)
        xs.sort()
        n = len(xs)
        means = [xs[int((n - 1) * i / (self.states - 1))] for i in range(self.states)] if self.states > 1 else [sum(xs) / n]
        sigma = max(math.sqrt(sum((x - sum(xs) / n) ** 2 for x in xs) / max(1, n)), 1e-5)
        sigmas = [sigma] * self.states
        trans = [[0.85 if i == j else 0.15 / (self.states - 1) for j in range(self.states)] for i in range(self.states)]
        posterior = [[1.0 / self.states] * self.states for _ in xs]
        for _ in range(self.iterations):
            # Forward-backward responsibilities; scaling is approximated by normalisation.
            f = [[0.0] * self.states for _ in xs]
            for j in range(self.states):
                f[0][j] = _normal_pdf(xs[0], means[j], sigmas[j]) / self.states
            for t in range(1, n):
                for j in range(self.states):
                    f[t][j] = _normal_pdf(xs[t], means[j], sigmas[j]) * sum(f[t - 1][i] * trans[i][j] for i in range(self.states))
                s = sum(f[t]) or 1.0
                f[t] = [v / s for v in f[t]]
            b = [[1.0] * self.states for _ in xs]
            for t in range(n - 2, -1, -1):
                for i in range(self.states):
                    b[t][i] = sum(trans[i][j] * _normal_pdf(xs[t + 1], means[j], sigmas[j]) * b[t + 1][j] for j in range(self.states))
                s = sum(b[t]) or 1.0
                b[t] = [v / s for v in b[t]]
            for t in range(n):
                q = [f[t][j] * b[t][j] for j in range(self.states)]
                s = sum(q) or 1.0
                posterior[t] = [v / s for v in q]
            for j in range(self.states):
                w = sum(posterior[t][j] for t in range(n)) or 1.0
                means[j] = sum(posterior[t][j] * xs[t] for t in range(n)) / w
                sigmas[j] = max(math.sqrt(sum(posterior[t][j] * (xs[t] - means[j]) ** 2 for t in range(n)) / w), 1e-5)
            for i in range(self.states):
                denom = sum(posterior[t][i] for t in range(n - 1)) or 1.0
                for j in range(self.states):
                    trans[i][j] = sum(posterior[t][i] * posterior[t + 1][j] for t in range(n - 1)) / denom
                s = sum(trans[i]) or 1.0
                trans[i] = [v / s for v in trans[i]]
        probs = posterior[-1]
        order = sorted(range(self.states), key=lambda i: means[i])
        rank = {state: idx for idx, state in enumerate(order)}
        probs = tuple(probs[i] for i in order)
        means = tuple(means[i] for i in order)
        sigmas = tuple(sigmas[i] for i in order)
        transition = tuple(tuple(trans[i][j] for j in order) for i in order)
        return HMMState(probs, max(range(self.states), key=probs.__getitem__), transition, means, sigmas)
