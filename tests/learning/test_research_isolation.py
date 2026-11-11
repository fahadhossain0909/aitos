"""Regression evidence for frozen research inputs; no services or orders."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aitos.learning.experiment_engine import (
    CostScenario,
    ExperimentEngine,
    ExperimentPlan,
    ExperimentVariant,
)
from aitos.learning.validation import CandidateValidator, ValidationPolicy


class CountingStrategy:
    def __init__(self):
        self.seen = []

    def __call__(self, event, execution):
        self.seen.append(event.timestamp)


@pytest.mark.parametrize("require_walk_forward", [True, False])
def test_candidate_validation_does_not_mutate_supplied_strategy(require_walk_forward):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        SimpleNamespace(timestamp=start + timedelta(days=i), fields={})
        for i in range(12)
    ]
    strategy = CountingStrategy()
    validator = CandidateValidator(
        ValidationPolicy(
            min_trades=0,
            require_walk_forward=require_walk_forward,
            train_days=3,
            test_days=2,
            holdout_days=3,
            min_positive_windows=0,
        )
    )
    first = validator.evaluate(events, strategy, lambda event: 100.0)
    assert strategy.seen == []
    second = validator.evaluate(events, strategy, lambda event: 100.0)
    assert first == second
    assert strategy.seen == []


def test_development_and_walk_forward_start_from_same_frozen_strategy(monkeypatch):
    starts = []

    class RecordingEngine:
        def __init__(self, *args):
            pass

        def run(self, events, strategy, mark_price):
            starts.append(len(strategy.seen))
            for event in events:
                strategy(event, self)
            return SimpleNamespace(
                metrics=SimpleNamespace(
                    total_return=0.1, max_drawdown=0.01, sharpe=1.0, trades=5
                )
            )

    monkeypatch.setattr("aitos.learning.validation.BacktestEngine", RecordingEngine)
    monkeypatch.setattr("aitos.learning.walk_forward.BacktestEngine", RecordingEngine)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [SimpleNamespace(timestamp=start + timedelta(days=i)) for i in range(12)]
    result = CandidateValidator(
        ValidationPolicy(train_days=3, test_days=2, holdout_days=3)
    ).evaluate(events, CountingStrategy(), lambda event: 100.0)
    assert result.passed
    assert len(starts) > 2  # development, sequential windows, locked holdout
    assert starts == [0] * len(starts)


def _plan():
    return ExperimentPlan(
        "isolation",
        ExperimentVariant("baseline"),
        (ExperimentVariant("ablation", ("ofi",)),),
        (CostScenario("base", 0.0004, 2.0),),
        minimum_samples=1,
    )


def _metrics(rows):
    return {
        "total_return": 0.1,
        "max_drawdown": 0.01,
        "sharpe": 1.0,
        "trades": len(rows),
        "sample_count": len(rows),
    }


def test_experiment_cells_cannot_mutate_each_others_nested_inputs():
    events = [
        {
            "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "regime": "trend",
            "fields": {"prices": [100.0, 101.0]},
        }
    ]
    original = deepcopy(events)
    inputs_seen = []

    def evaluator(rows, variant, cost, regime):
        inputs_seen.append(deepcopy(rows))
        rows[0]["fields"]["prices"].append(999.0)
        return _metrics(rows)

    report = ExperimentEngine().run(_plan(), events, evaluator)
    assert inputs_seen == [original, original]
    assert events == original
    pristine = ExperimentEngine().run(
        _plan(), events, lambda rows, *args: _metrics(rows)
    )
    assert report.dataset_fingerprint == pristine.dataset_fingerprint
    assert report.experiment_id == pristine.experiment_id


def test_experiment_identity_includes_sample_acceptance_policy():
    events = [
        {"timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc), "regime": "trend"}
    ]
    engine = ExperimentEngine()
    first = engine.run(_plan(), events, lambda rows, *args: _metrics(rows))
    strict = engine.run(
        replace(_plan(), minimum_samples=2), events, lambda rows, *args: _metrics(rows)
    )
    assert first.passed_sample_gates
    assert not strict.passed_sample_gates
    assert first.dataset_fingerprint == strict.dataset_fingerprint
    assert first.experiment_id != strict.experiment_id


def test_experiment_rejects_ambiguous_cost_scenario_names():
    with pytest.raises(ValueError, match="cost scenario names must be unique"):
        replace(
            _plan(),
            cost_scenarios=(
                CostScenario("same", 0.0004, 2.0),
                CostScenario("same", 0.0008, 8.0),
            ),
        )
