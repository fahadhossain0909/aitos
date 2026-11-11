"""Planning is an auditable research aid, not a validation/promotion result."""

from datetime import date, timedelta

import pytest

from aitos.learning.evaluation_plan import EvaluationPlanRequest, build_evaluation_plan


def request(**kw):
    return EvaluationPlanRequest(start="2025-01-01", end="2026-11-07", **kw)


def test_plan_has_rolling_train_windows_gaps_and_locked_holdout():
    report = build_evaluation_plan(request())
    assert report["fold_count"] > 1 and report["gap_days"] == 2
    holdout = date.fromisoformat(report["holdout"]["start"])
    for fold in report["folds"]:
        train_end = date.fromisoformat(fold["train_end"])
        test_start = date.fromisoformat(fold["test_start"])
        test_end = date.fromisoformat(fold["test_end"])
        assert (test_start - train_end).days == 2
        assert test_end <= holdout - timedelta(days=2)
        assert train_end - date.fromisoformat(fold["train_start"]) == timedelta(
            days=180
        )
    assert report["promotion_authorized"] is report["backtest_executed"] is False
    assert report["status"] == "planned_not_validated"


def test_short_history_is_not_a_passing_empty_backtest():
    report = build_evaluation_plan(EvaluationPlanRequest("2026-11-01", "2026-11-07"))
    assert report["status"] == "insufficient_history" and report["folds"] == []
    assert not report["holdout_fully_available"]


def test_plan_fingerprint_is_reproducible_and_sensitive_to_design():
    a = build_evaluation_plan(request())
    b = build_evaluation_plan(request())
    assert a["evidence_sha256"] == b["evidence_sha256"]
    assert (
        build_evaluation_plan(request(embargo_days=2))["evidence_sha256"]
        != a["evidence_sha256"]
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("train_days", True),
        ("test_days", 0),
        ("embargo_days", -1),
        ("label_horizon_days", 31),
        ("holdout_days", float("nan")),
        ("dataset_sha256", "fake"),
    ],
)
def test_invalid_design_is_rejected(key, value):
    with pytest.raises(ValueError):
        request(**{key: value})


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-11-07", "2026-11-07"),
        ("2026-11-07", "2025-01-01"),
        ("2025-02-30", "2026-11-07"),
        ("2025-01-01T00:00:00Z", "2026-11-07"),
    ],
)
def test_invalid_dates_are_rejected(start, end):
    with pytest.raises(ValueError):
        EvaluationPlanRequest(start, end)


def test_excessive_fold_count_is_bounded():
    with pytest.raises(ValueError, match="200-fold"):
        build_evaluation_plan(
            EvaluationPlanRequest("2000-01-01", "2026-11-07", train_days=1, test_days=1)
        )


def test_declared_hash_is_not_authenticated_dataset_evidence():
    assert (
        build_evaluation_plan(request(dataset_sha256="a" * 64))["dataset_evidence"]
        == "provided_not_verified"
    )
