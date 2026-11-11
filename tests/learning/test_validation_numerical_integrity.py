"""Non-finite research evidence and invalid temporal settings fail closed."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aitos.learning.validation import CandidateValidator, ValidationPolicy
from aitos.learning.walk_forward import WalkForwardValidator


def events():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        SimpleNamespace(timestamp=start + timedelta(days=i), fields={})
        for i in range(12)
    ]


def result(**overrides):
    values = dict(total_return=0.1, max_drawdown=0.02, sharpe=1.2, trades=10)
    values.update(overrides)
    return SimpleNamespace(metrics=SimpleNamespace(**values))


@pytest.mark.parametrize("field", ["min_total_return", "min_sharpe", "max_drawdown"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_policy_rejects_non_finite_thresholds(field, value):
    with pytest.raises(ValueError):
        ValidationPolicy(**{field: value})


@pytest.mark.parametrize(
    "field",
    ["train_days", "test_days", "holdout_days", "min_trades", "min_positive_windows"],
)
@pytest.mark.parametrize("value", [float("nan"), 1.5, True])
def test_policy_rejects_non_integer_counts_and_windows(field, value):
    with pytest.raises(ValueError):
        ValidationPolicy(**{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("total_return", float("nan")),
        ("sharpe", float("inf")),
        ("max_drawdown", float("nan")),
        ("trades", -1),
        ("trades", 1.5),
        ("trades", float("nan")),
    ],
)
def test_candidate_rejects_invalid_development_metrics(monkeypatch, field, value):
    class Engine:
        def __init__(self, *args):
            pass

        def run(self, *args):
            return result(**{field: value})

    monkeypatch.setattr("aitos.learning.validation.BacktestEngine", Engine)
    validation = CandidateValidator(
        ValidationPolicy(require_walk_forward=False)
    ).evaluate(events(), lambda *args: None, lambda event: 1.0)
    assert not validation.passed
    assert "invalid development metrics" in validation.reason


@pytest.mark.parametrize(
    "field,value",
    [("total_return", float("nan")), ("sharpe", float("inf")), ("trades", -1)],
)
def test_candidate_rejects_invalid_champion_evidence(monkeypatch, field, value):
    class Engine:
        def __init__(self, *args):
            pass

        def run(self, *args):
            return result()

    monkeypatch.setattr("aitos.learning.validation.BacktestEngine", Engine)
    validation = CandidateValidator(
        ValidationPolicy(require_walk_forward=False)
    ).evaluate(
        events(),
        lambda *args: None,
        lambda event: 1.0,
        champion_result=result(**{field: value}),
    )
    assert not validation.passed
    assert "invalid champion metrics" in validation.reason


@pytest.mark.timeout(1)
@pytest.mark.parametrize("field", ["train_days", "test_days", "holdout_days"])
@pytest.mark.parametrize("value", [0, -1, 0.5])
def test_direct_walk_forward_rejects_invalid_durations_before_loop(field, value):
    rows = events()
    arguments = dict(
        start=rows[0].timestamp,
        end=rows[-1].timestamp + timedelta(days=1),
        train_days=3,
        test_days=2,
        holdout_days=3,
    )
    arguments[field] = value
    with pytest.raises(ValueError):
        WalkForwardValidator().evaluate(
            rows, lambda *args: None, lambda event: 1.0, **arguments
        )


@pytest.mark.parametrize("stage", ["window", "holdout"])
def test_walk_forward_invalid_metrics_cannot_pass(monkeypatch, stage):
    class Engine:
        def __init__(self, *args):
            pass

        def run(self, rows, *args):
            is_holdout = min(row.timestamp.day for row in rows) >= 10
            if (stage == "holdout") == is_holdout:
                return result(sharpe=float("nan"))
            return result()

    monkeypatch.setattr("aitos.learning.walk_forward.BacktestEngine", Engine)
    rows = events()
    validation = WalkForwardValidator().evaluate(
        rows,
        lambda *args: None,
        lambda event: 1.0,
        start=rows[0].timestamp,
        end=rows[-1].timestamp + timedelta(days=1),
        train_days=3,
        test_days=2,
        holdout_days=3,
    )
    assert not validation.passed
    assert "invalid" in validation.reason


@pytest.mark.parametrize("field", ["total_return", "max_drawdown", "sharpe", "trades"])
@pytest.mark.parametrize("value", [None, True, "1.0", float("-inf")])
def test_non_numeric_or_non_finite_evidence_is_not_coerced(field, value):
    from aitos.learning.metric_integrity import metric_integrity_error

    assert metric_integrity_error(result(**{field: value}).metrics) is not None


@pytest.mark.parametrize("value", [-1, True, 1.5, float("nan"), float("inf")])
def test_direct_walk_forward_rejects_invalid_window_count(value):
    with pytest.raises(ValueError):
        WalkForwardValidator(min_positive_windows=value)


@pytest.mark.parametrize("boundary", ["start", "end", "reversed", "equal"])
def test_walk_forward_rejects_invalid_boundaries(boundary):
    rows = events()
    start, end = rows[0].timestamp, rows[-1].timestamp
    if boundary == "start":
        start = start.replace(tzinfo=None)
    elif boundary == "end":
        end = end.replace(tzinfo=None)
    elif boundary == "reversed":
        start, end = end, start
    else:
        end = start
    with pytest.raises(ValueError):
        WalkForwardValidator().evaluate(
            rows, lambda *args: None, lambda e: 1, start, end
        )


def test_walk_forward_rejects_mixed_timestamp_awareness_before_sorting():
    rows = events()
    start, end = rows[0].timestamp, rows[-1].timestamp
    rows[1].timestamp = rows[1].timestamp.replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        WalkForwardValidator().evaluate(
            rows, lambda *args: None, lambda e: 1, start, end
        )
