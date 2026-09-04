"""Extreme Value Theory via a lightweight Peaks-Over-Threshold GPD estimator."""

from __future__ import annotations

import math
from collections.abc import Sequence

from .models import EVTTail


class POTGPD:
    """Estimate right-tail loss severity from absolute negative returns."""

    def __init__(self, quantile: float = 0.95) -> None:
        if not 0.5 < quantile < 1.0:
            raise ValueError("quantile must be in (0.5, 1)")
        self.quantile = quantile

    def fit(self, returns: Sequence[float]) -> EVTTail:
        losses = sorted(max(0.0, -float(x)) for x in returns if math.isfinite(float(x)))
        n = len(losses)
        if n < 10:
            threshold = losses[max(0, int(0.8 * n) - 1)] if losses else 0.0
            return EVTTail(threshold, 0, 0.0, 0.0, max(threshold, 1e-8), 0.0, threshold)
        idx = min(n - 1, max(0, int(self.quantile * n) - 1))
        threshold = losses[idx]
        exc = [x - threshold for x in losses if x > threshold]
        k = len(exc)
        if k < 3 or sum(exc) <= 0:
            return EVTTail(
                threshold, k, k / n, 0.0, max(threshold, 1e-8), k / n, threshold
            )
        mean = sum(exc) / k
        second = sum(x * x for x in exc) / k
        # Method-of-moments GPD: var = beta^2 / (1-2xi).
        var = max(second - mean * mean, 1e-12)
        xi = 0.5 * (1.0 - (mean * mean / var))
        xi = max(-0.25, min(0.45, xi))
        scale = max(mean * (1.0 - xi), 1e-8)
        # Probability that a new loss exceeds the threshold plus one expected excess.
        tail_prob = k / n
        if xi < 0.0:
            expected_excess = scale / max(1.0 + xi, 1e-8)
        elif xi < 1.0:
            expected_excess = scale / max(1.0 - xi, 1e-8)
        else:
            expected_excess = scale * 10.0
        es = threshold + expected_excess
        return EVTTail(threshold, k, tail_prob, xi, scale, tail_prob, es)
