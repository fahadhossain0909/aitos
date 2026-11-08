"""Reproducible, cost-aware research experiments over the canonical backtest evaluator.

This module orchestrates comparisons; it never tunes, promotes, or deploys a
candidate. Callers must supply the canonical BacktestEngine-backed evaluator.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from math import isfinite
from typing import Any


@dataclass(frozen=True)
class CostScenario:
    name: str
    fee_rate: float
    slippage_bps: float

    def __post_init__(self) -> None:
        if (
            not self.name.strip()
            or not isfinite(self.fee_rate)
            or not isfinite(self.slippage_bps)
        ):
            raise ValueError("cost scenario fields must be finite and named")
        if self.fee_rate < 0 or self.slippage_bps < 0:
            raise ValueError("execution costs cannot be negative")


@dataclass(frozen=True)
class ExperimentVariant:
    name: str
    disabled_components: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("experiment variant name is required")
        if len(set(self.disabled_components)) != len(self.disabled_components):
            raise ValueError("disabled components must be unique")


@dataclass(frozen=True)
class ExperimentPlan:
    name: str
    baseline: ExperimentVariant
    variants: tuple[ExperimentVariant, ...]
    cost_scenarios: tuple[CostScenario, ...]
    regimes: tuple[str, ...] = ()
    minimum_samples: int = 30
    evidence_ids: tuple[str, ...] = ()
    holdout_start: datetime | None = None
    holdout_end: datetime | None = None

    def __post_init__(self) -> None:
        if not self.name.strip() or self.minimum_samples < 1:
            raise ValueError(
                "experiment name and positive minimum_samples are required"
            )
        names = [self.baseline.name, *(v.name for v in self.variants)]
        if len(set(names)) != len(names):
            raise ValueError("baseline and variant names must be unique")
        if not self.cost_scenarios:
            raise ValueError("at least one execution-cost scenario is required")
        if (self.holdout_start is None) != (self.holdout_end is None):
            raise ValueError("holdout boundaries must be supplied together")
        if self.holdout_start is not None:
            if self.holdout_start.tzinfo is None or self.holdout_end.tzinfo is None:
                raise ValueError("holdout boundaries must be timezone-aware")
            if self.holdout_end <= self.holdout_start:
                raise ValueError("holdout_end must be after holdout_start")

    @classmethod
    def from_hypothesis(
        cls,
        hypothesis: Any,
        cost_scenarios: tuple[CostScenario, ...],
        *,
        regimes: tuple[str, ...] = (),
        minimum_samples: int = 30,
    ) -> ExperimentPlan:
        """Turn an evidence-backed diagnostic into a bounded ablation plan."""
        component_by_category = {
            "adverse_order_flow": "order_flow",
            "regime_mismatch": "regime_filter",
            "execution_degraded": "execution_cost_model",
            "premature_exit": "exit_management",
            "missed_opportunity": "entry_filter",
        }
        category = str(getattr(hypothesis, "category", "")).strip().lower()
        component = component_by_category.get(category)
        if component is None:
            raise ValueError(
                f"no safe research ablation is defined for diagnostic: {category}"
            )
        evidence_ids = tuple(
            str(item) for item in (getattr(hypothesis, "evidence_ids", ()) or ())
        )
        if not evidence_ids:
            raise ValueError(
                "diagnostic hypothesis must retain supporting evidence IDs"
            )
        sample_count = int(getattr(hypothesis, "sample_count", 0) or 0)
        if sample_count < minimum_samples:
            raise ValueError(
                "diagnostic hypothesis does not meet experiment sample gate"
            )
        return cls(
            name=f"diagnostic-{category}",
            baseline=ExperimentVariant("baseline"),
            variants=(ExperimentVariant(f"without-{component}", (component,)),),
            cost_scenarios=cost_scenarios,
            regimes=regimes,
            minimum_samples=minimum_samples,
            evidence_ids=evidence_ids,
        )


@dataclass(frozen=True)
class ExperimentCell:
    regime: str
    cost_scenario: str
    variant: str
    metrics: Mapping[str, float]
    sample_count: int
    net_returns: tuple[float, ...] = ()
    hypothesis_p_value: float | None = None


@dataclass(frozen=True)
class ExperimentReport:
    experiment_id: str
    plan_name: str
    dataset_fingerprint: str
    cells: tuple[ExperimentCell, ...]
    deltas_vs_baseline: Mapping[str, Mapping[str, float]]
    passed_sample_gates: bool
    evidence_ids: tuple[str, ...]
    limitations: tuple[str, ...] = (
        "Research evidence only; no parameter tuning, strategy mutation or deployment.",
        "Holdout data must be reserved before experiment design and never reused for selection.",
    )
    statistical_validation: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "plan_name": self.plan_name,
            "dataset_fingerprint": self.dataset_fingerprint,
            "cells": [asdict(cell) for cell in self.cells],
            "deltas_vs_baseline": dict(self.deltas_vs_baseline),
            "passed_sample_gates": self.passed_sample_gates,
            "evidence_ids": list(self.evidence_ids),
            "limitations": list(self.limitations),
            "statistical_validation": dict(self.statistical_validation),
        }


Evaluator = Callable[
    [list[Any], ExperimentVariant, CostScenario, str], Mapping[str, Any]
]


class ExperimentEngine:
    """Run paired baseline/ablation/cost comparisons on identical event slices."""

    REQUIRED_METRICS = ("total_return", "max_drawdown", "sharpe", "trades")

    @staticmethod
    def _timestamp(event: Any) -> datetime:
        value = (
            event.get("timestamp")
            if isinstance(event, Mapping)
            else getattr(event, "timestamp", None)
        )
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("experiment events require timezone-aware timestamps")
        return value

    @staticmethod
    def _event_payload(event: Any) -> Any:
        if isinstance(event, Mapping):
            return dict(event)
        if hasattr(event, "__dict__"):
            return vars(event)
        return {
            "timestamp": ExperimentEngine._timestamp(event).isoformat(),
            "regime": ExperimentEngine._regime(event),
            "representation": repr(event),
        }

    @staticmethod
    def _regime(event: Any) -> str:
        value = (
            event.get("regime", "unknown")
            if isinstance(event, Mapping)
            else getattr(event, "regime", "unknown")
        )
        return str(value or "unknown").lower()

    @classmethod
    def _metrics(cls, raw: Mapping[str, Any]) -> tuple[dict[str, float], int]:
        values: dict[str, float] = {}
        for key in cls.REQUIRED_METRICS:
            try:
                value = float(raw[key])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"evaluator omitted numeric metric {key}") from exc
            if not isfinite(value):
                raise ValueError(f"metric {key} must be finite")
            values[key] = value
        count = int(raw.get("sample_count", values["trades"]))
        if count < 0:
            raise ValueError("sample_count cannot be negative")
        return values, count

    def run_hypotheses(
        self,
        hypotheses: Iterable[Any],
        events: Iterable[Any],
        evaluator: Evaluator,
        cost_scenarios: tuple[CostScenario, ...],
        *,
        regimes: tuple[str, ...] = (),
        minimum_samples: int = 30,
    ) -> tuple[ExperimentReport, ...]:
        """Generate and execute one bounded experiment per eligible diagnostic."""
        event_rows = list(events)
        reports = []
        for hypothesis in hypotheses:
            plan = ExperimentPlan.from_hypothesis(
                hypothesis,
                cost_scenarios,
                regimes=regimes,
                minimum_samples=minimum_samples,
            )
            reports.append(self.run(plan, event_rows, evaluator))
        return tuple(reports)

    def run(
        self,
        plan: ExperimentPlan,
        events: Iterable[Any],
        evaluator: Evaluator,
    ) -> ExperimentReport:
        ordered = sorted(list(events), key=self._timestamp)
        if not ordered:
            raise ValueError("experiment requires non-empty historical events")
        if plan.holdout_start is not None:
            # Only the explicitly reserved interval is evaluated. The caller
            # must keep this report out of candidate/parameter selection.
            ordered = [
                e
                for e in ordered
                if plan.holdout_start <= self._timestamp(e) < plan.holdout_end
            ]
            if not ordered:
                raise ValueError("reserved holdout contains no events")
        regimes = plan.regimes or tuple(sorted({self._regime(e) for e in ordered}))
        cells: list[ExperimentCell] = []
        baseline_index: dict[tuple[str, str], dict[str, float]] = {}
        deltas: dict[str, dict[str, float]] = {}
        passed = True
        variants = (plan.baseline, *plan.variants)
        for regime in regimes:
            regime_events = [e for e in ordered if self._regime(e) == regime]
            if not regime_events:
                passed = False
                continue
            for cost in plan.cost_scenarios:
                for variant in variants:
                    raw = evaluator(list(regime_events), variant, cost, regime)
                    metrics, count = self._metrics(raw)
                    raw_returns = tuple(
                        float(value) for value in raw.get("net_returns", ())
                    )
                    if raw_returns and (
                        len(raw_returns) != count
                        or any(not isfinite(value) for value in raw_returns)
                    ):
                        raise ValueError(
                            "net_returns must be finite and align with sample_count"
                        )
                    raw_p_value = raw.get("hypothesis_p_value")
                    p_value = (
                        float(raw_p_value) if raw_p_value is not None else None
                    )
                    if p_value is not None and (
                        not isfinite(p_value) or not 0.0 <= p_value <= 1.0
                    ):
                        raise ValueError("hypothesis_p_value must be in [0,1]")
                    cell = ExperimentCell(
                        regime,
                        cost.name,
                        variant.name,
                        metrics,
                        count,
                        raw_returns,
                        p_value,
                    )
                    cells.append(cell)
                    if count < plan.minimum_samples:
                        passed = False
                    if variant.name == plan.baseline.name:
                        baseline_index[(regime, cost.name)] = metrics
                    else:
                        base = baseline_index.get((regime, cost.name))
                        if base is not None:
                            key = f"{regime}|{cost.name}|{variant.name}"
                            deltas[key] = {
                                metric: metrics[metric] - base[metric]
                                for metric in self.REQUIRED_METRICS
                            }
        expected = len(regimes) * len(plan.cost_scenarios) * len(variants)
        if len(cells) != expected:
            passed = False
        statistical_validation: dict[str, Any] = {}
        from aitos.statistics.financial_ml_validation import validate_strategy_family

        for regime in regimes:
            for cost in plan.cost_scenarios:
                cohort_cells = [
                    cell
                    for cell in cells
                    if cell.regime == regime and cell.cost_scenario == cost.name
                ]
                if (
                    len(cohort_cells) < 2
                    or any(not cell.net_returns for cell in cohort_cells)
                    or len({len(cell.net_returns) for cell in cohort_cells}) != 1
                    or len(cohort_cells[0].net_returns) < 20
                ):
                    continue
                family_returns = {
                    cell.variant: cell.net_returns for cell in cohort_cells
                }
                family_p_values = {
                    cell.variant: cell.hypothesis_p_value for cell in cohort_cells
                }
                p_values = (
                    {
                        name: float(value)
                        for name, value in family_p_values.items()
                        if value is not None
                    }
                    if all(value is not None for value in family_p_values.values())
                    else None
                )
                family_report = validate_strategy_family(
                    family_returns,
                    hypothesis_p_values=p_values,
                    model_losses={
                        name: tuple(-value for value in values)
                        for name, values in family_returns.items()
                    },
                    resamples=250,
                    block_length=min(5, len(cohort_cells[0].net_returns)),
                    seed=0,
                )
                statistical_validation[f"{regime}|{cost.name}"] = asdict(
                    family_report
                )
        dataset_fingerprint = hashlib.sha256(
            json.dumps(
                [self._event_payload(event) for event in ordered],
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        canonical = {
            "name": plan.name,
            "dataset_fingerprint": dataset_fingerprint,
            "evidence_ids": sorted(plan.evidence_ids),
            "regimes": list(regimes),
            "costs": [asdict(c) for c in plan.cost_scenarios],
            "variants": [asdict(v) for v in variants],
            "holdout_start": (
                plan.holdout_start.isoformat() if plan.holdout_start else None
            ),
            "holdout_end": plan.holdout_end.isoformat() if plan.holdout_end else None,
        }
        experiment_id = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]
        return ExperimentReport(
            experiment_id,
            plan.name,
            dataset_fingerprint,
            tuple(cells),
            deltas,
            passed,
            plan.evidence_ids,
            statistical_validation=statistical_validation,
        )
