"""Leakage-aware statistical forecast validation utilities."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence


def _probability(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("probabilities must be finite and in [0, 1]")
    return value


def brier_score(probabilities: Iterable[float], outcomes: Iterable[int]) -> float:
    ps, ys = tuple(probabilities), tuple(outcomes)
    if not ps or len(ps) != len(ys):
        raise ValueError("probabilities and outcomes must have equal non-zero length")
    if any(int(y) not in (0, 1) for y in ys):
        raise ValueError("outcomes must be binary")
    return sum((_probability(p) - int(y)) ** 2 for p, y in zip(ps, ys)) / len(ps)


def log_loss(probabilities: Iterable[float], outcomes: Iterable[int],
             *, epsilon: float = 1e-15) -> float:
    ps, ys = tuple(probabilities), tuple(outcomes)
    if not ps or len(ps) != len(ys) or not 0 < epsilon < 0.5:
        raise ValueError("invalid inputs")
    loss = 0.0
    for p, y in zip(ps, ys):
        p = min(1.0 - epsilon, max(epsilon, _probability(p)))
        if int(y) not in (0, 1):
            raise ValueError("outcomes must be binary")
        loss -= int(y) * math.log(p) + (1 - int(y)) * math.log(1.0 - p)
    return loss / len(ps)


def qlike_loss(forecast_variance: Iterable[float],
               realised_proxy: Iterable[float]) -> float:
    forecasts, realised = tuple(forecast_variance), tuple(realised_proxy)
    if not forecasts or len(forecasts) != len(realised):
        raise ValueError("forecast and realised arrays must have equal non-zero length")
    losses = []
    for f, r in zip(forecasts, realised):
        f, r = float(f), float(r)
        if not math.isfinite(f) or not math.isfinite(r) or f <= 0 or r < 0:
            raise ValueError("variance forecasts must be positive; realised proxy non-negative")
        ratio = r / f
        losses.append(ratio - math.log(max(ratio, 1e-15)) - 1.0)
    return sum(losses) / len(losses)


@dataclass(frozen=True)
class TimeSeriesFold:
    train_indices: tuple[int, ...]
    test_indices: tuple[int, ...]


def purged_walk_forward_folds(length: int, *, n_splits: int = 5,
                              test_size: int | None = None,
                              label_horizon: int = 1,
                              embargo: int = 0) -> tuple[TimeSeriesFold, ...]:
    """Expanding-window folds with a label-overlap purge and post-test embargo.

    Indices are positional. For each test block, training is strictly earlier;
    label_horizon removes observations whose forward label could overlap the
    test block. This is intentionally conservative and does not implement CPCV.
    """
    if length <= 0 or n_splits <= 0 or label_horizon < 0 or embargo < 0:
        raise ValueError("invalid fold parameters")
    size = test_size or max(1, length // (n_splits + 1))
    first_test = length - n_splits * size
    if first_test <= 0:
        raise ValueError("not enough observations for requested folds")
    folds = []
    for split in range(n_splits):
        start = first_test + split * size
        end = min(length, start + size)
        train_end = max(0, start - label_horizon)
        train = tuple(range(train_end))
        # Embargo is applied to the immediately following observations; these
        # are not included in this fold's training set in any case.
        if not train or start >= end:
            continue
        folds.append(TimeSeriesFold(train, tuple(range(start, end))))
    return tuple(folds)
