"""Leakage-aware walk-forward evaluation for historical analogue evidence."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from aitos.intelligence.historical_analogue import search_historical_analogues
from aitos.models.market import Kline

_CLASSES = ("up", "down", "flat")


def _label(base: float, future: float, flat_band: float) -> str:
    if base <= 0:
        return "flat"
    change = future / base - 1
    return "up" if change > flat_band else "down" if change < -flat_band else "flat"


def _brier(probabilities: list[tuple[float, float, float]], labels: list[str]) -> float:
    if not labels:
        return 0.0
    return sum(
        sum(
            (p - (1.0 if cls == label else 0.0)) ** 2 for p, cls in zip(probs, _CLASSES)
        )
        for probs, label in zip(probabilities, labels)
    ) / len(labels)


def evaluate_historical_analogues_walk_forward(
    klines: Sequence[Kline],
    *,
    window: int = 20,
    horizon: int = 12,
    search_back: int = 500,
    top_k: int = 8,
    min_analogue_count: int = 3,
    step: int | None = None,
    flat_band: float = 0.001,
) -> dict[str, Any]:
    """Evaluate only predictions made at t against labels observed after t.

    The analogue matcher receives a prefix ending at t. Its own embargo ensures
    each matched pattern's complete forward outcome ends before the current
    pattern starts. OOS labels are then measured from t to t+horizon.
    """
    if (
        window < 5
        or horizon < 1
        or top_k < 1
        or min_analogue_count < 1
        or not math.isfinite(flat_band)
        or flat_band < 0
        or (step is not None and step < 1)
    ):
        raise ValueError("invalid walk-forward validation parameters")
    previous_time = None
    for row in klines:
        if not math.isfinite(float(row.close)) or float(row.close) <= 0:
            raise ValueError("walk-forward closes must be finite and positive")
        if previous_time is not None and row.open_time <= previous_time:
            raise ValueError("walk-forward candles must be strictly chronological")
        previous_time = row.open_time
    stride = step if step is not None else horizon
    first = max(window * 2 + horizon, window + min_analogue_count)
    probabilities = []
    labels = []
    timestamps = []
    for index in range(first, len(klines) - horizon, stride):
        prefix = klines[: index + 1]
        matches = search_historical_analogues(
            prefix,
            window=window,
            search_back=search_back,
            top_k=top_k,
            forward_horizon=horizon,
        )
        outcome = matches[0].outcome if matches else None
        if outcome is None or len(matches) < min_analogue_count:
            continue
        p = (
            float(outcome.up_probability),
            float(outcome.down_probability),
            float(outcome.flat_probability),
        )
        if (
            not all(math.isfinite(x) and 0 <= x <= 1 for x in p)
            or abs(sum(p) - 1) > 1e-6
        ):
            continue
        probabilities.append(p)
        labels.append(
            _label(klines[index].close, klines[index + horizon].close, flat_band)
        )
        timestamps.append(klines[index].close_time.isoformat())
    if not labels:
        return {
            "available": False,
            "reason": "insufficient_oos_predictions",
            "sample_count": 0,
            "window": window,
            "horizon": horizon,
            "step": stride,
            "leakage_policy": "prefix-only predictions; non-overlapping forward labels",
        }
    counts = {c: labels.count(c) for c in _CLASSES}
    priors = tuple(counts[c] / len(labels) for c in _CLASSES)
    brier = _brier(probabilities, labels)
    baseline = _brier([priors] * len(labels), labels)
    logloss = -sum(
        math.log(max(1e-12, p[_CLASSES.index(label)]))
        for p, label in zip(probabilities, labels)
    ) / len(labels)
    accuracy = sum(
        _CLASSES[max(range(3), key=lambda j: p[j])] == label
        for p, label in zip(probabilities, labels)
    ) / len(labels)
    return {
        "available": True,
        "sample_count": len(labels),
        "class_counts": counts,
        "brier_score": brier,
        "baseline_brier_score": baseline,
        "brier_skill_score": (1 - brier / baseline) if baseline > 0 else None,
        "log_loss": logloss,
        "directional_accuracy": accuracy,
        "mean_predicted_probabilities": {
            c: sum(p[i] for p in probabilities) / len(probabilities)
            for i, c in enumerate(_CLASSES)
        },
        "window": window,
        "horizon": horizon,
        "step": stride,
        "first_oos_time": timestamps[0],
        "last_oos_time": timestamps[-1],
        "leakage_policy": "prefix-only predictions; analogue forward horizons end before current window; non-overlapping OOS labels",
        "calibration_status": "evaluation_report_only_not_automatically_promoted",
    }
