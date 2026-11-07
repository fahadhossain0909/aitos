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
            raise ValueError(
                "variance forecasts must be positive; realised proxy non-negative"
            )
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
    """Expanding-window folds with a conservative pre-test purge gap.

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
        train_end = max(0, start - max(label_horizon, embargo))
        train = tuple(range(train_end))
        # Embargo is applied to the immediately following observations; these
        # are not included in this fold's training set in any case.
        if not train or start >= end:
            continue
        folds.append(TimeSeriesFold(train, tuple(range(start, end))))
    return tuple(folds)



@dataclass(frozen=True)
class CombinatorialFold:
    """Combinatorial purged-CV split over contiguous chronological groups."""

    train_indices: tuple[int, ...]
    test_indices: tuple[int, ...]
    test_groups: tuple[int, ...]


def combinatorial_purged_folds(
    length: int,
    *,
    n_groups: int = 8,
    n_test_groups: int = 2,
    purge: int = 0,
    embargo: int = 0,
) -> tuple[CombinatorialFold, ...]:
    """Generate CSCV-style test combinations with purge/embargo exclusions.

    Unlike walk-forward, combinatorial folds intentionally allow training
    observations on both sides of a test block. Use this for robustness and
    PBO diagnostics, never as a replacement for chronological deployment
    simulation.
    """
    from itertools import combinations

    if (
        length <= 0
        or n_groups < 2
        or not 1 <= n_test_groups < n_groups
        or purge < 0
        or embargo < 0
    ):
        raise ValueError("invalid combinatorial fold parameters")
    if n_groups > length:
        raise ValueError("n_groups cannot exceed the observation count")

    boundaries = [round(i * length / n_groups) for i in range(n_groups + 1)]
    groups = [tuple(range(boundaries[i], boundaries[i + 1])) for i in range(n_groups)]
    folds = []
    for selected in combinations(range(n_groups), n_test_groups):
        test = tuple(index for group in selected for index in groups[group])
        excluded = set(test)
        for group in selected:
            start, end = boundaries[group], boundaries[group + 1]
            excluded.update(range(max(0, start - purge), start))
            excluded.update(range(end, min(length, end + embargo)))
        train = tuple(index for index in range(length) if index not in excluded)
        if train and test:
            folds.append(CombinatorialFold(train, test, tuple(selected)))
    return tuple(folds)


def probability_of_backtest_overfitting(
    strategy_returns: Sequence[Sequence[float]],
    *,
    n_slices: int = 8,
) -> float:
    """Estimate PBO using symmetric combinatorially symmetric cross-validation.

    Input is strategy-major: one equal-length return series per strategy.
    The number of time observations must divide evenly into n_slices. For
    every half-slice in-sample combination, select the best in-sample strategy
    and measure its normalized rank out of sample. PBO is the fraction of
    combinations whose selected strategy ranks below the OOS median.
    """
    from itertools import combinations

    matrix = tuple(tuple(float(value) for value in row) for row in strategy_returns)
    if len(matrix) < 2 or n_slices < 2:
        raise ValueError("at least two strategies and two slices are required")
    width = len(matrix[0])
    if width == 0 or any(len(row) != width for row in matrix):
        raise ValueError("strategy return series must have equal non-zero length")
    if any(not math.isfinite(value) for row in matrix for value in row):
        raise ValueError("strategy returns must be finite")
    if width % n_slices:
        raise ValueError("observation count must be divisible by n_slices")

    slice_width = width // n_slices
    slices = [
        tuple(range(i * slice_width, (i + 1) * slice_width))
        for i in range(n_slices)
    ]
    half = n_slices // 2
    if n_slices % 2:
        raise ValueError("n_slices must be even for symmetric splitting")
    below_median = 0
    total = 0
    for in_sample_groups in combinations(range(n_slices), half):
        in_set = set(in_sample_groups)
        in_indices = tuple(i for g in in_sample_groups for i in slices[g])
        out_indices = tuple(
            i for g in range(n_slices) if g not in in_set for i in slices[g]
        )
        in_scores = [mean(matrix[s][i] for i in in_indices) for s in range(len(matrix))]
        selected = max(range(len(matrix)), key=in_scores.__getitem__)
        out_scores = [
            mean(matrix[s][i] for i in out_indices)
            for s in range(len(matrix))
        ]
        selected_score = out_scores[selected]
        rank = sum(score <= selected_score for score in out_scores)
        normalized_rank = rank / (len(out_scores) + 1.0)
        if normalized_rank < 0.5:
            below_median += 1
        total += 1
    return below_median / total if total else 0.0
