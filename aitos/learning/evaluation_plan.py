"""Deterministic research partition planning; no backtest, tuning or promotion.

Conservative gaps reserve label-horizon + embargo days BEFORE each unseen test
and before the locked holdout. This is explicitly a date plan, not evidence that
actual event/label intervals are purged or that a dataset is leakage-free.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class EvaluationPlanRequest:
    start: str
    end: str
    train_days: int = 180
    test_days: int = 30
    holdout_days: int = 30
    label_horizon_days: int = 1
    embargo_days: int = 1
    dataset_sha256: str | None = None

    def __post_init__(self):
        for key, minimum, maximum in (
            ("train_days", 1, 3650),
            ("test_days", 1, 365),
            ("holdout_days", 1, 365),
            ("label_horizon_days", 0, 30),
            ("embargo_days", 0, 30),
        ):
            value = getattr(self, key)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"invalid bounded {key}")
        for key in ("start", "end"):
            value = getattr(self, key)
            if not isinstance(value, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", value
            ):
                raise ValueError("boundaries require ISO calendar dates")
            date.fromisoformat(value)
        start, end = date.fromisoformat(self.start), date.fromisoformat(self.end)
        if not 0 < (end - start).days <= 36525:
            raise ValueError("invalid bounded research date span")
        if self.dataset_sha256 is not None and (
            not isinstance(self.dataset_sha256, str)
            or not re.fullmatch(r"[a-f0-9]{64}", self.dataset_sha256)
        ):
            raise ValueError("dataset SHA256 must be lowercase hexadecimal")


def build_evaluation_plan(request: EvaluationPlanRequest) -> dict:
    start, end = date.fromisoformat(request.start), date.fromisoformat(request.end)
    gap = timedelta(days=request.label_horizon_days + request.embargo_days)
    train = timedelta(days=request.train_days)
    test = timedelta(days=request.test_days)
    holdout_start = end - timedelta(days=request.holdout_days)
    development_test_end = holdout_start - gap
    cursor = start + train + gap
    folds = []
    while cursor + test <= development_test_end:
        if len(folds) >= 200:
            raise ValueError("research plan exceeds 200-fold bound")
        train_end = cursor - gap
        folds.append(
            dict(
                index=len(folds) + 1,
                train_start=(train_end - train).isoformat(),
                train_end=train_end.isoformat(),
                gap_start=train_end.isoformat(),
                test_start=cursor.isoformat(),
                test_end=(cursor + test).isoformat(),
            )
        )
        cursor += test
    inputs = {k: getattr(request, k) for k in request.__dataclass_fields__}
    core = dict(
        schema_version=1,
        engine="conservative-walk-forward-plan-v1",
        scope="partition_plan_only",
        inputs=inputs,
        timezone="UTC",
        intervals="half_open_start_inclusive_end_exclusive",
        gap_policy="pre_test_label_horizon_plus_embargo",
        gap_days=gap.days,
        folds=folds,
        fold_count=len(folds),
        holdout=dict(start=max(start, holdout_start).isoformat(), end=end.isoformat()),
        holdout_fully_available=holdout_start >= start,
        development_test_end=development_test_end.isoformat(),
        unused_development_days=max(0, (development_test_end - cursor).days),
        status="planned_not_validated" if folds else "insufficient_history",
        dataset_evidence=(
            "provided_not_verified" if request.dataset_sha256 else "not_supplied"
        ),
        backtest_executed=False,
        promotion_authorized=False,
    )
    digest = hashlib.sha256(
        json.dumps(
            core, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    return {
        **core,
        "evidence_sha256": digest,
        "limitations": [
            "Date planning only: no dataset, event labels, strategy or returns were evaluated.",
            "Gaps assume the declared maximum label horizon; actual intervals must be checked independently.",
            "Training windows may reuse earlier test periods in later folds, as in chronological walk-forward research.",
            "Candidate selection and parameter tuning must not inspect the locked holdout.",
            "No trial-count correction, statistical significance, fee/slippage adequacy or profitable edge is certified.",
        ],
    }
