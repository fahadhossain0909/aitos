"""Leakage-aware statistical forecast validation utilities."""

from __future__ import annotations

import math
import random
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from statistics import mean


def _probability(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("probabilities must be finite and in [0, 1]")
    return value


def brier_score(probabilities: Iterable[float], outcomes: Iterable[int]) -> float:
    ps, ys = tuple(probabilities), tuple(outcomes)
    if not ps or len(ps) != len(ys):
        raise ValueError("probabilities and outcomes must have equal non-zero length")
    if any(y not in (0, 1) for y in ys):
        raise ValueError("outcomes must be binary")
    return sum((_probability(p) - int(y)) ** 2 for p, y in zip(ps, ys)) / len(ps)


def log_loss(
    probabilities: Iterable[float],
    outcomes: Iterable[int],
    *,
    epsilon: float = 1e-15,
) -> float:
    ps, ys = tuple(probabilities), tuple(outcomes)
    if not ps or len(ps) != len(ys) or not 0 < epsilon < 0.5:
        raise ValueError("invalid inputs")
    loss = 0.0
    for p, y in zip(ps, ys):
        p = min(1.0 - epsilon, max(epsilon, _probability(p)))
        if y not in (0, 1):
            raise ValueError("outcomes must be binary")
        loss -= int(y) * math.log(p) + (1 - int(y)) * math.log(1.0 - p)
    return loss / len(ps)


def qlike_loss(
    forecast_variance: Iterable[float], realised_proxy: Iterable[float]
) -> float:
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


def purged_walk_forward_folds(
    length: int,
    *,
    n_splits: int = 5,
    test_size: int | None = None,
    label_horizon: int = 1,
    embargo: int = 0,
) -> tuple[TimeSeriesFold, ...]:
    """Expanding-window folds with a conservative pre-test purge gap.

    Indices are positional. For each test block, training is strictly earlier;
    label_horizon removes observations whose forward label could overlap the
    test block. This is intentionally conservative and does not implement CPCV.
    """
    if length <= 0 or n_splits <= 0 or label_horizon < 0 or embargo < 0:
        raise ValueError("invalid fold parameters")
    if test_size is not None and test_size <= 0:
        raise ValueError("test_size must be positive")
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
        tuple(range(i * slice_width, (i + 1) * slice_width)) for i in range(n_slices)
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
            mean(matrix[s][i] for i in out_indices) for s in range(len(matrix))
        ]
        selected_score = out_scores[selected]
        rank = sum(score <= selected_score for score in out_scores)
        normalized_rank = rank / (len(out_scores) + 1.0)
        if normalized_rank < 0.5:
            below_median += 1
        total += 1
    return below_median / total if total else 0.0


def expected_calibration_error(
    probabilities: Iterable[float],
    outcomes: Iterable[int],
    *,
    n_bins: int = 10,
) -> float:
    """Return equal-width expected calibration error for binary forecasts."""
    ps, ys = tuple(probabilities), tuple(outcomes)
    if not ps or len(ps) != len(ys) or n_bins <= 0:
        raise ValueError("invalid calibration inputs")
    if any(y not in (0, 1) for y in ys):
        raise ValueError("outcomes must be binary")
    bins: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]
    for probability, outcome in zip(ps, ys):
        p = _probability(probability)
        index = min(n_bins - 1, int(p * n_bins))
        bins[index].append((p, int(outcome)))
    total = len(ps)
    return sum(
        len(bucket)
        / total
        * abs(
            sum(p for p, _ in bucket) / len(bucket)
            - sum(y for _, y in bucket) / len(bucket)
        )
        for bucket in bins
        if bucket
    )


@dataclass(frozen=True)
class BootstrapInterval:
    """Paired block-bootstrap confidence interval for mean return improvement."""

    estimate: float
    lower: float
    upper: float
    confidence: float
    resamples: int
    block_length: int
    sample_count: int


def _linear_quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - lower_index
    return ordered[lower_index] + fraction * (
        ordered[upper_index] - ordered[lower_index]
    )


def paired_block_bootstrap_difference(
    candidate_returns: Iterable[float],
    incumbent_returns: Iterable[float],
    *,
    block_length: int = 5,
    n_resamples: int = 2000,
    confidence: float = 0.95,
    seed: int = 0,
) -> BootstrapInterval:
    """Estimate candidate-minus-incumbent OOS return using paired time blocks.

    Resampling paired differences in contiguous blocks preserves short-range
    serial dependence better than an IID bootstrap. It does not correct
    strategy-selection bias; use purged folds/PBO and a truly untouched OOS
    period before feeding the lower bound into model-promotion governance.
    """
    candidate = tuple(float(value) for value in candidate_returns)
    incumbent = tuple(float(value) for value in incumbent_returns)
    if not candidate or len(candidate) != len(incumbent):
        raise ValueError("paired return samples must be non-empty and equal length")
    if any(not math.isfinite(value) for value in (*candidate, *incumbent)):
        raise ValueError("paired return samples must be finite")
    if block_length <= 0 or block_length > len(candidate):
        raise ValueError("block_length must be in [1, sample_count]")
    if n_resamples < 100:
        raise ValueError("n_resamples must be at least 100")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be in (0, 1)")
    differences = tuple(a - b for a, b in zip(candidate, incumbent))
    estimate = mean(differences)
    rng = random.Random(seed)
    block_count = math.ceil(len(differences) / block_length)
    samples = []
    max_start = len(differences) - block_length
    for _ in range(n_resamples):
        resampled = []
        for _ in range(block_count):
            start = rng.randrange(max_start + 1)
            resampled.extend(differences[start : start + block_length])
        samples.append(mean(resampled[: len(differences)]))
    tail_probability = (1.0 - confidence) / 2.0
    return BootstrapInterval(
        estimate=estimate,
        lower=_linear_quantile(samples, tail_probability),
        upper=_linear_quantile(samples, 1.0 - tail_probability),
        confidence=confidence,
        resamples=n_resamples,
        block_length=block_length,
        sample_count=len(differences),
    )


@dataclass(frozen=True)
class FeatureDriftResult:
    feature: str
    psi: float
    threshold: float
    drifted: bool
    reference_count: int
    current_count: int


@dataclass(frozen=True)
class DriftReport:
    features: tuple[FeatureDriftResult, ...]
    max_psi: float
    drifted_features: tuple[str, ...]

    @property
    def drifted(self) -> bool:
        return bool(self.drifted_features)


def population_stability_index(
    reference: Iterable[float],
    current: Iterable[float],
    *,
    bins: int = 10,
    epsilon: float = 1e-6,
) -> float:
    """Compute PSI using reference-quantile bins with smoothed proportions."""
    reference_values = tuple(float(value) for value in reference)
    current_values = tuple(float(value) for value in current)
    if not reference_values or not current_values:
        raise ValueError("reference and current samples must be non-empty")
    if any(not math.isfinite(value) for value in (*reference_values, *current_values)):
        raise ValueError("drift samples must be finite")
    if bins < 2:
        raise ValueError("bins must be at least 2")
    if not math.isfinite(epsilon) or not 0.0 < epsilon < 0.1:
        raise ValueError("epsilon must be finite and in (0, 0.1)")
    ordered = sorted(reference_values)
    edges = sorted(
        {
            ordered[min(len(ordered) - 1, math.ceil(i * len(ordered) / bins) - 1)]
            for i in range(1, bins)
        }
    )
    if len(edges) == 1:
        center = edges[0]
        width = max(abs(center) * 1e-6, 1e-6)
        edges = [center - width, center + width]
    bucket_count = len(edges) + 1
    reference_counts = [0] * bucket_count
    current_counts = [0] * bucket_count
    for value in reference_values:
        reference_counts[bisect_right(edges, value)] += 1
    for value in current_values:
        current_counts[bisect_right(edges, value)] += 1
    reference_total = len(reference_values)
    current_total = len(current_values)
    reference_proportions = [
        max(epsilon, count / reference_total) for count in reference_counts
    ]
    current_proportions = [
        max(epsilon, count / current_total) for count in current_counts
    ]
    reference_norm = sum(reference_proportions)
    current_norm = sum(current_proportions)
    reference_proportions = [p / reference_norm for p in reference_proportions]
    current_proportions = [p / current_norm for p in current_proportions]
    return sum(
        (current_p - reference_p) * math.log(current_p / reference_p)
        for reference_p, current_p in zip(reference_proportions, current_proportions)
    )


def assess_feature_drift(
    reference: Mapping[str, Iterable[float]],
    current: Mapping[str, Iterable[float]],
    *,
    threshold: float = 0.2,
    bins: int = 10,
) -> DriftReport:
    """Return per-feature PSI and a max-PSI drift score."""
    if not reference or set(reference) != set(current):
        raise ValueError("reference and current feature names must match")
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("drift threshold must be finite and positive")
    results = []
    for feature in sorted(reference):
        reference_values = tuple(reference[feature])
        current_values = tuple(current[feature])
        psi = population_stability_index(reference_values, current_values, bins=bins)
        results.append(
            FeatureDriftResult(
                feature=feature,
                psi=psi,
                threshold=threshold,
                drifted=psi >= threshold,
                reference_count=len(reference_values),
                current_count=len(current_values),
            )
        )
    ordered = tuple(sorted(results, key=lambda item: item.psi, reverse=True))
    return DriftReport(
        features=ordered,
        max_psi=max(item.psi for item in ordered),
        drifted_features=tuple(item.feature for item in ordered if item.drifted),
    )


@dataclass(frozen=True)
class ExecutionCostScenario:
    cost_multiplier: float
    total_cost_bps: float
    net_expected_return: float
    gross_to_cost_ratio: float | None


@dataclass(frozen=True)
class ExecutionCostSensitivity:
    gross_expected_return: float
    base_cost_bps: float
    scenarios: tuple[ExecutionCostScenario, ...]


def execution_cost_sensitivity(
    expected_gross_return: float,
    *,
    entry_fee_bps: float,
    exit_fee_bps: float,
    spread_bps: float,
    slippage_bps: float,
    funding_bps: float = 0.0,
    borrow_bps: float = 0.0,
    stress_multipliers: Iterable[float] = (1.0, 1.5, 2.0),
) -> ExecutionCostSensitivity:
    """Evaluate gross-to-net edge under explicit execution-cost stress.

    All cost inputs are non-negative basis points. Spread, slippage, funding
    and borrow are supplied as total round-trip costs for the forecast horizon;
    callers must not double-count entry/exit components. Expected return is a
    decimal return aligned to the proposed position side. The result is a
    research diagnostic and does not submit orders or alter hard risk limits.
    """
    gross = float(expected_gross_return)
    components = (
        entry_fee_bps,
        exit_fee_bps,
        spread_bps,
        slippage_bps,
        funding_bps,
        borrow_bps,
    )
    costs = tuple(float(value) for value in components)
    if not math.isfinite(gross):
        raise ValueError("expected_gross_return must be finite")
    if any(not math.isfinite(value) or value < 0.0 for value in costs):
        raise ValueError("execution costs must be finite and non-negative")
    multipliers = tuple(float(value) for value in stress_multipliers)
    if not multipliers or any(
        not math.isfinite(value) or value < 1.0 for value in multipliers
    ):
        raise ValueError("stress multipliers must be finite and >= 1")
    base_cost_bps = sum(costs)
    scenarios = []
    for multiplier in multipliers:
        total_cost_bps = base_cost_bps * multiplier
        cost_return = total_cost_bps / 10_000.0
        scenarios.append(
            ExecutionCostScenario(
                cost_multiplier=multiplier,
                total_cost_bps=total_cost_bps,
                net_expected_return=gross - cost_return,
                gross_to_cost_ratio=(
                    None if cost_return == 0.0 else gross / cost_return
                ),
            )
        )
    return ExecutionCostSensitivity(
        gross_expected_return=gross,
        base_cost_bps=base_cost_bps,
        scenarios=tuple(scenarios),
    )


@dataclass(frozen=True)
class CohortValidationMetric:
    cohort_type: str
    cohort: str
    observations: int
    brier_score_value: float
    log_loss_value: float
    directional_hit_rate: float
    sufficiently_sampled: bool


def regime_symbol_validation(
    probabilities: Sequence[float],
    outcomes: Sequence[int],
    *,
    symbols: Sequence[str],
    regimes: Sequence[str],
    minimum_observations: int = 30,
) -> tuple[CohortValidationMetric, ...]:
    """Report chronological OOS forecast metrics by symbol and regime.

    Inputs must already be strictly out-of-sample predictions generated by
    purged walk-forward/CPCV folds. This function does not fit models or repair
    leakage; it preserves small cohorts but marks them under-sampled.
    """
    ps = tuple(_probability(value) for value in probabilities)
    raw_outcomes = tuple(outcomes)
    if any(value not in (0, 1) for value in raw_outcomes):
        raise ValueError("outcomes must be binary")
    ys = tuple(int(value) for value in raw_outcomes)
    symbol_values = tuple(str(value) for value in symbols)
    regime_values = tuple(str(value) for value in regimes)
    if not ps or len({len(ps), len(ys), len(symbol_values), len(regime_values)}) != 1:
        raise ValueError("probabilities, outcomes, symbols and regimes must align")
    if any(not value for value in symbol_values + regime_values):
        raise ValueError("symbol and regime labels must be non-empty")
    if minimum_observations <= 0:
        raise ValueError("minimum_observations must be positive")
    reports = []
    for cohort_type, labels in (("symbol", symbol_values), ("regime", regime_values)):
        for label in sorted(set(labels)):
            indices = [i for i, value in enumerate(labels) if value == label]
            cohort_ps = [ps[i] for i in indices]
            cohort_ys = [ys[i] for i in indices]
            reports.append(
                CohortValidationMetric(
                    cohort_type=cohort_type,
                    cohort=label,
                    observations=len(indices),
                    brier_score_value=brier_score(cohort_ps, cohort_ys),
                    log_loss_value=log_loss(cohort_ps, cohort_ys),
                    directional_hit_rate=sum(
                        (probability >= 0.5) == bool(outcome)
                        for probability, outcome in zip(cohort_ps, cohort_ys)
                    )
                    / len(indices),
                    sufficiently_sampled=len(indices) >= minimum_observations,
                )
            )
    return tuple(reports)


@dataclass(frozen=True)
class MulticlassCalibrationReport:
    observations: int
    classes: int
    brier_score: float
    log_loss: float
    expected_calibration_error: float


def multiclass_calibration_report(
    probabilities: Sequence[Sequence[float]],
    outcomes: Sequence[int],
    *,
    bins: int = 10,
) -> MulticlassCalibrationReport:
    """Evaluate causal OOS multiclass posterior calibration."""
    rows = tuple(tuple(float(value) for value in row) for row in probabilities)
    raw_labels = tuple(outcomes)
    labels = tuple(int(value) for value in raw_labels)
    if any(raw != label for raw, label in zip(raw_labels, labels)):
        raise ValueError("outcome labels must be integers")
    if not rows or len(rows) != len(labels) or bins < 2:
        raise ValueError("probabilities/outcomes must align and bins must be >=2")
    classes = len(rows[0])
    if classes < 2 or any(len(row) != classes for row in rows):
        raise ValueError("posterior rows must have a consistent class count >=2")
    if any(
        not math.isfinite(value) or not 0.0 <= value <= 1.0
        for row in rows
        for value in row
    ):
        raise ValueError("posterior probabilities must be finite and in [0,1]")
    if any(abs(sum(row) - 1.0) > 1e-6 for row in rows):
        raise ValueError("each posterior row must sum to one")
    if any(label < 0 or label >= classes for label in labels):
        raise ValueError("outcome class is outside posterior dimensions")
    brier = sum(
        sum((value - float(index == label)) ** 2 for index, value in enumerate(row))
        for row, label in zip(rows, labels)
    ) / len(rows)
    logloss = -sum(
        math.log(max(1e-15, row[label])) for row, label in zip(rows, labels)
    ) / len(rows)
    confidence = [max(row) for row in rows]
    correct = [
        int(max(range(classes), key=row.__getitem__) == label)
        for row, label in zip(rows, labels)
    ]
    ece = 0.0
    for bin_index in range(bins):
        lower = bin_index / bins
        upper = (bin_index + 1) / bins
        indices = [
            index
            for index, value in enumerate(confidence)
            if lower <= value < upper or (bin_index == bins - 1 and value == 1.0)
        ]
        if indices:
            average_confidence = mean(confidence[index] for index in indices)
            average_accuracy = mean(correct[index] for index in indices)
            ece += len(indices) / len(rows) * abs(average_confidence - average_accuracy)
    return MulticlassCalibrationReport(len(rows), classes, brier, logloss, ece)
