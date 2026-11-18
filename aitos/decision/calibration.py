"""Online, bucketed probability calibration and proper scoring metrics."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque


@dataclass(frozen=True)
class CalibrationMetrics:
    samples: int
    brier_score: float | None
    log_loss: float | None
    expected_calibration_error: float | None


class ProbabilityCalibrator:
    """One-vs-rest reliability-bucket calibrator with a cold-start guard.

    Update once per class for each matured, point-in-time outcome. The caller is
    responsible for chronological label maturity and leakage-free train windows.
    """

    def __init__(
        self,
        *,
        bins: int = 10,
        minimum_samples: int = 30,
        prior_strength: float = 2.0,
        max_history: int = 10000,
    ) -> None:
        if bins < 2 or minimum_samples < 1 or prior_strength <= 0 or max_history < 1:
            raise ValueError("invalid calibration configuration")
        self.bins = bins
        self.minimum_samples = minimum_samples
        self.prior_strength = prior_strength
        self._counts: dict[str, list[int]] = defaultdict(lambda: [0] * bins)
        self._successes: dict[str, list[int]] = defaultdict(lambda: [0] * bins)
        self._history: Deque[tuple[str, float, bool]] = deque(maxlen=max_history)

    def _bucket(self, probability: float) -> int:
        return min(self.bins - 1, int(probability * self.bins))

    def update(self, label: str, probability: float, observed: bool) -> None:
        if not label:
            raise ValueError("calibration label is required")
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError("probability must be finite and between zero and one")
        if not isinstance(observed, bool):
            raise ValueError("observed outcome must be boolean")
        bucket = self._bucket(probability)
        self._counts[label][bucket] += 1
        self._successes[label][bucket] += int(observed)
        self._history.append((label, probability, observed))

    def calibrate(self, label: str, probability: float) -> tuple[float, bool]:
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError("probability must be finite and between zero and one")
        bucket = self._bucket(probability)
        count = self._counts[label][bucket]
        if count < self.minimum_samples:
            return probability, False
        successes = self._successes[label][bucket]
        strength = self.prior_strength
        value = (successes + strength * probability) / (count + strength)
        return min(1.0, max(0.0, value)), True

    def metrics(self, label: str | None = None) -> CalibrationMetrics:
        rows = [row for row in self._history if label is None or row[0] == label]
        if not rows:
            return CalibrationMetrics(0, None, None, None)
        brier = 0.0
        log_loss = 0.0
        bins: dict[int, list[float]] = defaultdict(list)
        epsilon = 1e-12
        for _, probability, observed in rows:
            target = float(observed)
            brier += (probability - target) ** 2
            clipped = min(1.0 - epsilon, max(epsilon, probability))
            log_loss -= target * math.log(clipped) + (1.0 - target) * math.log(
                1.0 - clipped
            )
            bins[self._bucket(probability)].append(target - probability)
        ece = sum(
            len(errors) * abs(sum(errors) / len(errors))
            for errors in bins.values()
        ) / len(rows)
        return CalibrationMetrics(
            len(rows), brier / len(rows), log_loss / len(rows), ece
        )
