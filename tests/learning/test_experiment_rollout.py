from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aitos.learning.experiment_engine import (
    CostScenario,
    ExperimentEngine,
    ExperimentPlan,
    ExperimentVariant,
)
from aitos.learning.governance import (
    PromotionApproval,
    validation_evidence_digest,
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
    assert len(report.dataset_fingerprint) == 64


def test_experiment_engine_rejects_naive_timestamps():
    events = [{"timestamp": datetime(2026, 1, 1), "regime": "trend"}]
    plan = ExperimentPlan(
        "bad-data",
        ExperimentVariant("baseline"),
        (),
        (CostScenario("base", 0.0, 0.0),),
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
        self.champion = SimpleNamespace(version=version)
        return SimpleNamespace(version=version)

    def promote(
        self,
        name,
        version,
        *,
        validation_result,
        approval,
        champion_comparison=None,
    ):
        approval.validate(
            candidate_name=name,
            candidate_version=version,
            validation_digest=validation_evidence_digest(
                validation_result, champion_comparison
            ),
        )
        self.champion = SimpleNamespace(version=version)
        return SimpleNamespace(version=version, parent_version="v1")


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


def test_canary_requires_minimum_sample_count_and_observations(tmp_path):
    coordinator = RolloutCoordinator(
        str(tmp_path / "rollouts.json"),
        FakeRegistry(),
        canary_router=lambda *args: None,
        production_router=lambda *args: None,
        rollback_router=lambda *args: None,
        minimum_canary_samples=10,
        minimum_canary_observations=2,
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
        category="unknown",
        sample_count=100,
        evidence_ids=("trade-1",),
    )
    with pytest.raises(ValueError, match="no safe research ablation"):
        ExperimentPlan.from_hypothesis(unsupported, (CostScenario("base", 0.0, 0.0),))
    weak = SimpleNamespace(
        category="adverse_order_flow",
        sample_count=2,
        evidence_ids=("trade-1",),
    )
    with pytest.raises(ValueError, match="sample gate"):
        ExperimentPlan.from_hypothesis(
            weak, (CostScenario("base", 0.0, 0.0),), minimum_samples=10
        )


def test_run_hypotheses_generates_and_executes_experiments():
    hypothesis = SimpleNamespace(
        category="premature_exit",
        sample_count=40,
        evidence_ids=("closed-trade-7",),
    )
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        {"timestamp": start + timedelta(days=i), "regime": "trend"} for i in range(40)
    ]

    def evaluate(rows, variant, cost, regime):
        return {
            "total_return": 0.1,
            "max_drawdown": 0.03,
            "sharpe": 1.0,
            "trades": len(rows),
            "sample_count": len(rows),
        }

    reports = ExperimentEngine().run_hypotheses(
        [hypothesis],
        events,
        evaluate,
        (CostScenario("base", 0.0004, 2.0),),
    )
    assert len(reports) == 1
    assert reports[0].plan_name == "diagnostic-premature_exit"


def test_healthy_canary_requires_approval_then_supports_rollback(tmp_path):
    registry = FakeRegistry()
    routed = []
    coordinator = RolloutCoordinator(
        str(tmp_path / "rollouts.json"),
        registry,
        canary_router=lambda *args: routed.append(("canary", *args)),
        production_router=lambda *args: routed.append(("production", *args)),
        rollback_router=lambda *args: routed.append(("rollback", *args)),
        minimum_canary_samples=5,
        minimum_canary_observations=3,
    )
    validation = _validation()
    coordinator.stage("strategy", "v2", validation)
    coordinator.start_canary("strategy", "v2")
    for count in (5, 6, 7):
        coordinator.observe_canary(
            "strategy",
            "v2",
            sample_count=count,
            error_rate=0.0,
            drawdown=0.01,
            return_delta=0.02,
        )
    approval = PromotionApproval(
        candidate_name="strategy",
        candidate_version="v2",
        approved_by="operator:test",
        approval_id="rollout-approval-v2",
        validation_digest=validation_evidence_digest(validation),
        approved_at=datetime.now(timezone.utc),
    )
    promoted = coordinator.promote("strategy", "v2", validation, approval)
    assert promoted.version == "v2"
    assert coordinator.get("strategy", "v2")["state"] == "promoted"
    restored = coordinator.rollback("strategy", "v2", "canary regression")
    assert restored.version == "v1"
    assert coordinator.get("strategy", "v2")["state"] == "rolled_back"
    assert routed[-1] == ("rollback", "strategy", "v1")


def test_canary_rollback_failure_is_persisted_for_manual_reconciliation(
    tmp_path,
):
    def fail_rollback(*args):
        raise RuntimeError("router unavailable")

    coordinator = RolloutCoordinator(
        str(tmp_path / "rollouts.json"),
        FakeRegistry(),
        canary_router=lambda *args: None,
        production_router=lambda *args: None,
        rollback_router=fail_rollback,
        minimum_canary_samples=1,
        minimum_canary_observations=1,
    )
    coordinator.stage("strategy", "v2", _validation())
    coordinator.start_canary("strategy", "v2")
    row = coordinator.observe_canary(
        "strategy",
        "v2",
        sample_count=1,
        error_rate=0.5,
        drawdown=0.5,
        return_delta=-0.5,
    )
    assert row["state"] == "rollback_pending"
    assert (
        coordinator.get("strategy", "v2")["rollback_reason"]
        == "canary_rollback_route_failed"
    )


def test_experiment_engine_runs_reality_check_fdr_and_mcs_on_net_returns():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        {"timestamp": start + timedelta(days=i), "regime": "trend"} for i in range(120)
    ]
    plan = ExperimentPlan(
        name="family-validation",
        baseline=ExperimentVariant("primary"),
        variants=(ExperimentVariant("challenger"),),
        cost_scenarios=(CostScenario("base", 0.0004, 2.0),),
        minimum_samples=30,
    )

    def evaluate(rows, variant, cost, regime):
        offset = 0.001 if variant.name == "primary" else 0.0005
        returns = [offset + (index % 7) * 0.0001 for index in range(len(rows))]
        return {
            "total_return": sum(returns),
            "max_drawdown": 0.02,
            "sharpe": 1.2,
            "trades": len(rows),
            "sample_count": len(rows),
            "net_returns": returns,
            "hypothesis_p_value": 0.01 if variant.name == "primary" else 0.04,
        }

    report = ExperimentEngine().run(plan, events, evaluate)
    family = report.statistical_validation["trend|base"]
    assert family["family_size"] == 2
    assert family["reality_check"]["resamples"] == 1000
    assert family["adjusted_p_values"]["method"] == "bh"
    assert family["model_confidence_set"] is not None
    assert "trend|base" in report.to_dict()["statistical_validation"]
