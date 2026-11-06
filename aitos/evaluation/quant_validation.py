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


def validate_observations(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
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
        row.update(
            decision_at=decision_at, outcome_at=outcome_at, side=side, net_pnl=pnl
        )
        for key in (
            "mae_pct",
            "mfe_pct",
            "entry_capture_pct",
            "slippage_bps",
            "fees_usd",
            "funding_usd",
            "counterfactual_net_pnl",
        ):
            if row.get(key) is not None:
                value = float(row[key])
                if not isfinite(value):
                    raise ValueError(f"row {index}: {key} must be finite")
                row[key] = value
        row["regime"] = str(row.get("regime") or "unknown")
        row["rejected_opportunity"] = bool(row.get("rejected_opportunity", False))
        validated.append(row)
    return tuple(sorted(validated, key=lambda item: item["decision_at"]))


def chronological_walk_forward_splits(
    rows: Iterable[Mapping[str, Any]],
    *,
    train_fraction: float = 0.6,
    test_fraction: float = 0.2,
    step_fraction: float = 0.2,
    embargo: timedelta = timedelta(0),
) -> tuple[EvaluationFold, ...]:
    """Create expanding chronological folds with an explicit leakage embargo."""
    data = validate_observations(rows)
    count = len(data)
    if (
        not 0 < train_fraction < 1
        or not 0 < test_fraction < 1
        or not 0 < step_fraction <= 1
    ):
        raise ValueError("fractions must be in (0,1), with step_fraction <= 1")
    if embargo < timedelta(0):
        raise ValueError("embargo cannot be negative")
    train_size = max(1, int(count * train_fraction))
    test_size = max(1, int(count * test_fraction))
    step = max(1, int(count * step_fraction))
    folds = []
    for train_end_idx in range(train_size, count - test_size + 1, step):
        test_start_idx = train_end_idx
        test_start = data[test_start_idx]["decision_at"]
        eligible_train = tuple(
            i
            for i in range(train_end_idx)
            if data[i]["outcome_at"] + embargo <= test_start
        )
        if not eligible_train:
            continue
        train_end = data[eligible_train[-1]]["decision_at"]
        test_indices = tuple(
            range(test_start_idx, min(count, test_start_idx + test_size))
        )
        if not eligible_train or not test_indices:
            continue
        folds.append(
            EvaluationFold(
                train_start=data[eligible_train[0]]["decision_at"],
                train_end=train_end,
                test_start=test_start,
                test_end=data[test_indices[-1]]["decision_at"],
                train_indices=eligible_train,
                test_indices=test_indices,
            )
        )
    return tuple(folds)


def _side_metrics(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    groups: dict[str, list[dict[str, float]]] = defaultdict(list)
    for row in rows:
        groups[str(row["side"]).upper()].append({"net_pnl": float(row["net_pnl"])})
    return {side: walk_forward_metrics(items) for side, items in sorted(groups.items())}


def _optional_metric(
    rows: Iterable[Mapping[str, Any]], key: str
) -> float | None:
    values = [float(row[key]) for row in rows if row.get(key) is not None]
    if not values:
        return None
    if any(not isfinite(value) for value in values):
        raise ValueError(f"{key} values must be finite")
    return sum(values) / len(values)


def _regime_metrics(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("regime") or "unknown")].append(row)
    return {
        regime: {
            "metrics": walk_forward_metrics(items),
            "long_short": _side_metrics(items),
            "trade_count": len(items),
        }
        for regime, items in sorted(groups.items())
    }


def _research_diagnostics(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    data = tuple(rows)
    return {
        "mean_mae_pct": _optional_metric(data, "mae_pct"),
        "mean_mfe_pct": _optional_metric(data, "mfe_pct"),
        "mean_entry_capture_pct": _optional_metric(data, "entry_capture_pct"),
        "mean_slippage_bps": _optional_metric(data, "slippage_bps"),
        "mean_fees_usd": _optional_metric(data, "fees_usd"),
        "mean_funding_usd": _optional_metric(data, "funding_usd"),
        "samples": len(data),
    }


def expectancy_confidence_interval(
    rows: Iterable[Mapping[str, Any]], confidence_z: float = 1.96
) -> dict[str, float | int | None]:
    """Normal-approximation interval for mean net PnL; not a profitability test."""
    values = [float(row["net_pnl"]) for row in rows]
    if any(not isfinite(value) for value in values):
        raise ValueError("net_pnl values must be finite")
    if not isfinite(confidence_z) or confidence_z <= 0:
        raise ValueError("confidence_z must be finite and positive")
    count = len(values)
    if count < 2:
        return {
            "samples": count,
            "mean": sum(values) / count if count else None,
            "lower": None,
            "upper": None,
            "confidence_z": confidence_z,
        }
    mean = sum(values) / count
    variance = sum((value - mean) ** 2 for value in values) / (count - 1)
    margin = confidence_z * (variance**0.5) / (count**0.5)
    return {
        "samples": count,
        "mean": mean,
        "lower": mean - margin,
        "upper": mean + margin,
        "confidence_z": confidence_z,
    }


def rejected_opportunity_counterfactual(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Summarize realized markouts for rejected opportunities, when supplied."""
    rejected = [
        row
        for row in rows
        if bool(row.get("rejected_opportunity"))
        and row.get("counterfactual_net_pnl") is not None
    ]
    pnl = [float(row["counterfactual_net_pnl"]) for row in rejected]
    if any(not isfinite(value) for value in pnl):
        raise ValueError("counterfactual_net_pnl values must be finite")
    missed = sum(value > 0 for value in pnl)
    return {
        "samples": len(pnl),
        "counterfactual_metrics": walk_forward_metrics(
            [{"net_pnl": value} for value in pnl]
        ),
        "positive_markout_rate": missed / len(pnl) if pnl else 0.0,
        "negative_markout_rate": (
            sum(value < 0 for value in pnl) / len(pnl) if pnl else 0.0
        ),
    }


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
            "by_regime": _regime_metrics(items),
            "research_diagnostics": _research_diagnostics(items),
            "expectancy_confidence_interval": expectancy_confidence_interval(items),
            "rejected_opportunity_counterfactual": rejected_opportunity_counterfactual(items),
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
                "expectancy": result["overall"]["expectancy"]
                - baseline["overall"]["expectancy"],
                "profit_factor": (
                    (
                        result["overall"]["profit_factor"]
                        - baseline["overall"]["profit_factor"]
                    )
                    if result["overall"]["profit_factor"] is not None
                    and baseline["overall"]["profit_factor"] is not None
                    else None
                ),
                "max_drawdown": result["overall"]["max_drawdown"]
                - baseline["overall"]["max_drawdown"],
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
        results.append(
            {
                "fold": index,
                "train_end": fold.train_end.isoformat(),
                "test_start": fold.test_start.isoformat(),
                "test_end": fold.test_end.isoformat(),
                "test_metrics": walk_forward_metrics(test),
                "long_short": _side_metrics(test),
                "by_regime": _regime_metrics(test),
                "research_diagnostics": _research_diagnostics(test),
                "expectancy_confidence_interval": expectancy_confidence_interval(test),
                "test_trade_count": len(test),
            }
        )
    return tuple(results)
