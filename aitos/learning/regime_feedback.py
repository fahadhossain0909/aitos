"""Regime-partitioned learning and outcome-quality feedback primitives."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from math import isfinite, sqrt
from statistics import median
from typing import Iterable


def normalize_regime(value: object) -> str:
    key = str(value or "unknown").strip().lower().replace(" ", "_")
    return key if key and len(key) <= 48 else "unknown"


class RegimeRewardCalibrator:
    """Robustly clip reward outliers within each market-regime partition."""

    def __init__(self, *, history_size: int = 500, min_samples: int = 20) -> None:
        if history_size < 1 or min_samples < 3:
            raise ValueError("invalid reward calibration window")
        self._history = defaultdict(lambda: deque(maxlen=history_size))
        self._min_samples = min_samples

    def calibrate(self, regime: str, reward_r: float) -> float:
        if not isfinite(reward_r):
            raise ValueError("reward must be finite")
        history = self._history[normalize_regime(regime)]
        if len(history) < self._min_samples:
            calibrated = max(-5.0, min(5.0, reward_r))
        else:
            center = median(history)
            mad = median(abs(item - center) for item in history)
            scale = max(1.4826 * mad, 0.25)
            calibrated = max(center - 3.0 * scale, min(center + 3.0 * scale, reward_r))
        history.append(float(reward_r))
        return float(calibrated)

    def snapshot(self) -> dict[str, list[float]]:
        return {key: list(values) for key, values in self._history.items()}

    def restore(self, values: dict[str, Iterable[float]]) -> None:
        for regime, rewards in values.items():
            key = normalize_regime(regime)
            clean = [float(value) for value in rewards if isfinite(float(value))]
            self._history[key].extend(clean[-self._history[key].maxlen :])


@dataclass(frozen=True)
class OutcomeFeedback:
    premature_exit: bool
    missed_opportunity_r: float
    realized_r: float
    mfe_r: float


def outcome_feedback(
    *, realized_r: float, mfe_r: float, exit_reason: str = ""
) -> OutcomeFeedback:
    """Quantify avoidable early exits without treating MFE as guaranteed PnL."""
    if not isfinite(realized_r) or not isfinite(mfe_r) or mfe_r < 0:
        raise ValueError("outcome R values must be finite and MFE non-negative")
    missed = max(0.0, mfe_r - realized_r)
    explicit_early_reason = any(
        token in str(exit_reason).lower()
        for token in ("manual", "time", "stagnation", "early")
    )
    premature = mfe_r >= 1.0 and realized_r < 0.5 * mfe_r and explicit_early_reason
    return OutcomeFeedback(premature, missed, realized_r, mfe_r)


@dataclass(frozen=True)
class ModelComparison:
    accepted: bool
    candidate_mean: float
    champion_mean: float
    standard_error: float
    reason: str


def compare_champion_candidate(
    champion_rewards: Iterable[float],
    candidate_rewards: Iterable[float],
    *,
    min_samples: int = 30,
    min_improvement: float = 0.05,
    z_score: float = 1.96,
) -> ModelComparison:
    """Conservative independent-sample acceptance gate for shadow evaluation."""
    champion = [float(v) for v in champion_rewards if isfinite(float(v))]
    candidate = [float(v) for v in candidate_rewards if isfinite(float(v))]
    if min_samples < 2 or min_improvement < 0 or z_score <= 0:
        raise ValueError("invalid champion/challenger gate configuration")
    if len(champion) < min_samples or len(candidate) < min_samples:
        return ModelComparison(False, 0.0, 0.0, float("inf"), "insufficient samples")
    c_mean = sum(champion) / len(champion)
    n_mean = sum(candidate) / len(candidate)
    c_var = sum((v - c_mean) ** 2 for v in champion) / (len(champion) - 1)
    n_var = sum((v - n_mean) ** 2 for v in candidate) / (len(candidate) - 1)
    se = sqrt(c_var / len(champion) + n_var / len(candidate))
    lower_bound = n_mean - c_mean - z_score * se
    accepted = lower_bound > min_improvement
    reason = (
        "candidate clears confidence-bound improvement gate"
        if accepted
        else "candidate improvement is not statistically established"
    )
    return ModelComparison(accepted, n_mean, c_mean, se, reason)
