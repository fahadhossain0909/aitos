from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aitos.learning.experiment_engine import (
    CostScenario,
    ExperimentEngine,
    ExperimentPlan,
    ExperimentVariant,
)
from aitos.learning.rollout import RolloutCoordinator


def _validation():
    metrics = SimpleNamespace(
        total_return=0.12,
        max_drawdown=0.04,
        sharpe=1.4,
        trades=80,
        initial_equity=10000.0,
        final_equity=11200.0,
    )
    window = SimpleNamespace(
        test_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        test_end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        result=SimpleNamespace(metrics=metrics),
    )
    walk = SimpleNamespace(
        passed=True,
        locked_holdout=SimpleNamespace(metrics=metrics),
        windows=[window],
        reason="validated",
    )
    return SimpleNamespace(
        passed=True,
        reason="passed",
        candidate=SimpleNamespace(metrics=metrics),
        champion=None,
        walk_forward=walk,
    )


def test_experiment_engine_runs_paired_ablation_and_cost_sensitivity():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        {"timestamp": start + timedelta(days=i), "regime": "trend"} for i in range(40)
    ]
    plan = ExperimentPlan(
        name="ofi-ablation",
        baseline=ExperimentVariant("full"),
        variants=(ExperimentVariant("without_ofi", ("ofi",)),),
        cost_scenarios=(
            CostScenario("base", 0.0004, 2.0),
            CostScenario("stress", 0.0008, 8.0),
        ),
        minimum_samples=30,
        evidence_ids=("diag-1", "trade-2"),
    )

    def evaluate(rows, variant, cost, regime):
        penalty = (
            0.02 if "ofi" in variant.disabled_components else 0.0
        ) + cost.slippage_bps / 1000
        return {
            "total_return": 0.2 - penalty,
            "max_drawdown": 0.05 + penalty,
            "sharpe": 1.5 - penalty,
            "trades": len(rows),
            "sample_count": len(rows),
        }

    report = ExperimentEngine().run(plan, events, evaluate)
    assert report.passed_sample_gates
    assert len(report.cells) == 4
    assert report.deltas_vs_baseline["trend|base|without_ofi"]["total_return"] < 0
    assert len(report.experiment_id) == 24


def test_experiment_engine_rejects_naive_timestamps():
    events = [{"timestamp": datetime(2026, 1, 1), "regime": "trend"}]
    plan = ExperimentPlan(
        "bad-data", ExperimentVariant("baseline"), (), (CostScenario("base", 0.0, 0.0),)
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        ExperimentEngine().run(plan, events, lambda *args: {})


class FakeRegistry:
    def __init__(self):
        self.artifact = SimpleNamespace(status="candidate", version="v2")
        self.champion = SimpleNamespace(version="v1")
        self.rollbacks = []

    def get_artifact(self, name, version):
        return self.artifact if version == "v2" else None

    def get_champion(self, name):
        return self.champion

    def rollback(self, name, version, *, reason):
        self.rollbacks.append((name, version, reason))
        return SimpleNamespace(version=version)


def test_canary_threshold_breach_rolls_back_and_persists_state(tmp_path):
    registry, routed = FakeRegistry(), []
    coordinator = RolloutCoordinator(
        str(tmp_path / "rollouts.json"),
        registry,
        canary_router=lambda n, v, f: routed.append(("canary", n, v, f)),
        production_router=lambda n, v: routed.append(("production", n, v)),
        rollback_router=lambda n, v: routed.append(("rollback", n, v)),
        minimum_canary_samples=10,
    )
    coordinator.stage("strategy", "v2", _validation())
    coordinator.start_canary("strategy", "v2", 0.05)
    row = coordinator.observe_canary(
        "strategy",
        "v2",
        sample_count=10,
        error_rate=0.08,
        drawdown=0.03,
        return_delta=0.01,
    )
    assert row["state"] == "rolled_back"
    assert routed[-1] == ("rollback", "strategy", "v1")
    assert (
        coordinator.get("strategy", "v2")["rollback_reason"]
        == "canary_threshold_breach"
    )


def test_canary_requires_minimum_sample_count_before_healthy(tmp_path):
    coordinator = RolloutCoordinator(
        str(tmp_path / "rollouts.json"),
        FakeRegistry(),
        canary_router=lambda *args: None,
        production_router=lambda *args: None,
        rollback_router=lambda *args: None,
        minimum_canary_samples=10,
    )
    coordinator.stage("strategy", "v2", _validation())
    coordinator.start_canary("strategy", "v2")
    row = coordinator.observe_canary(
        "strategy",
        "v2",
        sample_count=9,
        error_rate=0.0,
        drawdown=0.01,
        return_delta=0.0,
    )
    assert row["state"] == "canary"
    row = coordinator.observe_canary(
        "strategy",
        "v2",
        sample_count=10,
        error_rate=0.0,
        drawdown=0.01,
        return_delta=0.0,
    )
    assert row["state"] == "canary_healthy"



def test_experiment_plan_is_generated_from_evidence_backed_diagnostic():
    hypothesis = SimpleNamespace(
        category="adverse_order_flow",
        sample_count=45,
        evidence_ids=("trade-1", "trade-2"),
    )
    plan = ExperimentPlan.from_hypothesis(
        hypothesis, (CostScenario("base", 0.0004, 2.0),)
    )
    assert plan.name == "diagnostic-adverse_order_flow"
    assert plan.variants[0].disabled_components == ("order_flow",)
    assert plan.evidence_ids == ("trade-1", "trade-2")


def test_experiment_plan_rejects_unsupported_or_weak_diagnostics():
    unsupported = SimpleNamespace(
        category="unknown", sample_count=100, evidence_ids=("trade-1",)
    )
    with pytest.raises(ValueError, match="no safe research ablation"):
        ExperimentPlan.from_hypothesis(
            unsupported, (CostScenario("base", 0.0, 0.0),)
        )
    weak = SimpleNamespace(
        category="adverse_order_flow", sample_count=2, evidence_ids=("trade-1",)
    )
    with pytest.raises(ValueError, match="sample gate"):
        ExperimentPlan.from_hypothesis(
            weak, (CostScenario("base", 0.0, 0.0),), minimum_samples=10
        )
