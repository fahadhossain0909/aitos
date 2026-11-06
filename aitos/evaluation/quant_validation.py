"""Leakage-aware historical evaluation primitives for strategy research.

These functions consume already-recorded, point-in-time trade observations.
They do not fabricate market data or execute orders. The caller is responsible
for generating observations with the canonical backtest/replay engine.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from typing import Any, Iterable, Mapping

from aitos.intelligence.adaptive_mtf import walk_forward_metrics


@dataclass(frozen=True, slots=True)
class EvaluationFold:
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    train_indices: tuple[int, ...]
    test_indices: tuple[int, ...]


def _time(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("timestamps must be datetime or ISO-8601 strings")
    if result.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return result


def validate_observations(rows: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Validate chronological, finite observations and point-in-time boundaries."""
    validated = []
    for index, raw in enumerate(rows):
        row = dict(raw)
        decision_at = _time(row["decision_at"])
        outcome_at = _time(row["outcome_at"])
        if outcome_at <= decision_at:
            raise ValueError(f"row {index}: outcome_at must follow decision_at")
        side = str(row["side"]).upper()
        if side not in {"LONG", "SHORT"}:
            raise ValueError(f"row {index}: side must be LONG or SHORT")
        pnl = float(row["net_pnl"])
        if not isfinite(pnl):
            raise ValueError(f"row {index}: net_pnl must be finite")
        if not str(row.get("variant", "baseline")):
            raise ValueError(f"row {index}: variant cannot be empty")
        row.update(decision_at=decision_at, outcome_at=outcome_at, side=side, net_pnl=pnl)
        validated.append(row)
    return tuple(sorted(validated, key=lambda item: item["decision_at"]))


def chronological_walk_forward_splits(
    rows: Iterable[Mapping[str, Any]], *, train_fraction: float = 0.6,
    test_fraction: float = 0.2, step_fraction: float = 0.2,
    embargo: timedelta = timedelta(0),
) -> tuple[EvaluationFold, ...]:
    """Create expanding chronological folds with an explicit leakage embargo."""
    data = validate_observations(rows)
    count = len(data)
    if not 0 < train_fraction < 1 or not 0 < test_fraction < 1 or not 0 < step_fraction <= 1:
        raise ValueError("fractions must be in (0,1), with step_fraction <= 1")
    if embargo < timedelta(0):
        raise ValueError("embargo cannot be negative")
    train_size = max(1, int(count * train_fraction))
    test_size = max(1, int(count * test_fraction))
    step = max(1, int(count * step_fraction))
    folds = []
    for train_end_idx in range(train_size, count - test_size + 1, step):
        test_start_idx = train_end_idx
        train_end = data[train_end_idx - 1]["decision_at"]
        test_start = data[test_start_idx]["decision_at"]
        eligible_train = tuple(
            i for i in range(train_end_idx)
            if data[i]["outcome_at"] + embargo <= test_start
        )
        test_indices = tuple(range(test_start_idx, min(count, test_start_idx + test_size)))
        if not eligible_train or not test_indices:
            continue
        folds.append(EvaluationFold(
            train_start=data[eligible_train[0]]["decision_at"],
            train_end=train_end,
            test_start=test_start,
            test_end=data[test_indices[-1]]["decision_at"],
            train_indices=eligible_train,
            test_indices=test_indices,
        ))
    return tuple(folds)


def _side_metrics(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    groups: dict[str, list[dict[str, float]]] = defaultdict(list)
    for row in rows:
        groups[str(row["side"]).upper()].append({"net_pnl": float(row["net_pnl"])})
    return {side: walk_forward_metrics(items) for side, items in sorted(groups.items())}


def comparative_report(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Report baseline/ablation variants and LONG/SHORT outcomes separately."""
    data = validate_observations(rows)
    variants: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in data:
        variants[str(row.get("variant", "baseline"))].append(row)
    report: dict[str, Any] = {}
    for variant, items in sorted(variants.items()):
        report[variant] = {
            "overall": walk_forward_metrics(items),
            "by_side": _side_metrics(items),
            "trade_count": len(items),
            "date_range": {
                "start": items[0]["decision_at"].isoformat(),
                "end": items[-1]["decision_at"].isoformat(),
            },
        }
    baseline = report.get("baseline")
    if baseline is not None:
        for variant, result in report.items():
            if variant == "baseline":
                continue
            result["delta_vs_baseline"] = {
                "expectancy": result["overall"]["expectancy"] - baseline["overall"]["expectancy"],
                "profit_factor": result["overall"]["profit_factor"] - baseline["overall"]["profit_factor"],
                "max_drawdown": result["overall"]["max_drawdown"] - baseline["overall"]["max_drawdown"],
            }
    return report


def evaluate_fold_results(
    rows: Iterable[Mapping[str, Any]], folds: Iterable[EvaluationFold]
) -> tuple[dict[str, Any], ...]:
    """Summarize only held-out test indices; training observations never score."""
    data = validate_observations(rows)
    results = []
    for index, fold in enumerate(folds):
        test = [data[i] for i in fold.test_indices]
        results.append({
            "fold": index,
            "train_end": fold.train_end.isoformat(),
            "test_start": fold.test_start.isoformat(),
            "test_end": fold.test_end.isoformat(),
            "test_metrics": walk_forward_metrics(test),
            "long_short": _side_metrics(test),
            "test_trade_count": len(test),
        })
    return tuple(results)
