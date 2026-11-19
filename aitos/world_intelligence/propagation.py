"""Cross-asset event propagation and lead/lag diagnostics.

This module evaluates observed event-window returns only. It does not infer
causality from correlation; results are stratified by regime and bootstrapped
with deterministic seeds for reproducibility.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass
from math import isfinite, sqrt
from typing import Sequence


@dataclass(frozen=True, slots=True)
class PropagationSample:
    event_id: str
    source_asset: str
    target_asset: str
    regime: str
    source_return: float
    target_return: float
    lag_seconds: float

    def __post_init__(self):
        if not all(
            isinstance(v, str) and v.strip()
            for v in (self.event_id, self.source_asset, self.target_asset, self.regime)
        ):
            raise ValueError("event, asset and regime identities are required")
        if self.source_asset.upper() == self.target_asset.upper():
            raise ValueError("source and target assets must differ")
        for name in ("source_return", "target_return", "lag_seconds"):
            value = float(getattr(self, name))
            if not isfinite(value):
                raise ValueError(f"{name} must be finite")
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class PropagationEstimate:
    source_asset: str
    target_asset: str
    regime: str
    sample_count: int
    return_correlation: float | None
    correlation_ci: tuple[float, float] | None
    mean_target_return: float | None
    target_return_ci: tuple[float, float] | None
    mean_lag_seconds: float | None
    lag_ci: tuple[float, float] | None
    lead_lag_label: str


def _corr(xs, ys):
    if len(xs) < 3:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sqrt(vx * vy)


def _bootstrap_ci(values, *, seed, iterations=1000, confidence=0.95, statistic=None):
    if not values:
        return None
    rng = random.Random(seed)
    fn = statistic or (lambda sample: sum(sample) / len(sample))
    estimates = []
    for _ in range(iterations):
        sample = [values[rng.randrange(len(values))] for _ in values]
        result = fn(sample)
        if result is not None and isfinite(result):
            estimates.append(result)
    estimates.sort()
    alpha = (1.0 - confidence) / 2.0
    return (
        (
            estimates[int(alpha * (len(estimates) - 1))],
            estimates[int((1.0 - alpha) * (len(estimates) - 1))],
        )
        if estimates
        else None
    )


def validate_event_propagation(
    samples: Sequence[PropagationSample],
    *,
    min_samples: int = 30,
    max_abs_lag_seconds: float = 86_400.0,
    iterations: int = 1000,
    confidence: float = 0.95,
    seed: int = 17,
) -> tuple[PropagationEstimate, ...]:
    if min_samples < 3 or iterations < 100 or not 0.5 < confidence < 1:
        raise ValueError("invalid propagation validation policy")
    if not isfinite(max_abs_lag_seconds) or max_abs_lag_seconds <= 0:
        raise ValueError("max_abs_lag_seconds must be positive")
    grouped = defaultdict(list)
    seen = set()
    for sample in samples:
        key = (
            sample.event_id,
            sample.source_asset.upper(),
            sample.target_asset.upper(),
            sample.regime,
        )
        if key in seen:
            raise ValueError("duplicate event propagation sample")
        seen.add(key)
        if abs(sample.lag_seconds) <= max_abs_lag_seconds:
            grouped[
                (
                    sample.source_asset.upper(),
                    sample.target_asset.upper(),
                    sample.regime,
                )
            ].append(sample)
    reports = []
    for (source, target, regime), rows in sorted(grouped.items()):
        if len(rows) < min_samples:
            continue
        xs = [r.source_return for r in rows]
        ys = [r.target_return for r in rows]
        lags = [r.lag_seconds for r in rows]
        corr = _corr(xs, ys)
        corr_ci = _bootstrap_ci(
            rows,
            seed=seed,
            iterations=iterations,
            confidence=confidence,
            statistic=lambda rs: _corr(
                [r.source_return for r in rs], [r.target_return for r in rs]
            ),
        )
        mean_target = sum(ys) / len(ys)
        target_ci = _bootstrap_ci(
            ys, seed=seed + 1, iterations=iterations, confidence=confidence
        )
        lag_mean = sum(lags) / len(lags)
        lag_ci = _bootstrap_ci(
            lags, seed=seed + 2, iterations=iterations, confidence=confidence
        )
        label = (
            "source_leads_target"
            if lag_mean > 0
            else "target_leads_source" if lag_mean < 0 else "synchronous"
        )
        reports.append(
            PropagationEstimate(
                source,
                target,
                regime,
                len(rows),
                corr,
                corr_ci,
                mean_target,
                target_ci,
                lag_mean,
                lag_ci,
                label,
            )
        )
    return tuple(reports)


__all__ = ["PropagationEstimate", "PropagationSample", "validate_event_propagation"]
