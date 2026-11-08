from datetime import datetime, timedelta, timezone

import pytest

from aitos.learning.continuous_intelligence import (
    ContinuousIntelligenceEngine,
    DriftDetector,
)
from aitos.learning.governance import (
    PromotionApproval,
    validation_evidence_digest,
)


def _trade(
    experience_id,
    *,
    source="paper",
    reward=1.0,
    strategy="momentum",
    regime="trend",
    metadata=None,
):
    return {
        "experience_id": experience_id,
        "source": source,
        "symbol": "BTCUSDT",
        "decision": "LONG",
        "outcome": "closed",
        "reward": reward,
        "strategy_version": strategy,
        "market_state": {"regime": regime},
        "risk_state": {"risk_amount_usd": 10.0},
        "metadata": {"reward_unit": "r_multiple", **(metadata or {})},
    }


def test_performance_attribution_groups_strategy_regime_and_symbol_side():
    report = ContinuousIntelligenceEngine().analyze(
        [
            _trade("a", reward=1.0),
            _trade("b", reward=-0.5),
            _trade("c", reward=2.0, regime="range"),
        ]
    )

    assert report.eligible_trades == 3
    assert report.rejected_experiences == 0
    assert len(report.performance_by_strategy_regime) == 2
    trend = next(
        row
        for row in report.performance_by_strategy_regime
        if row.key == "momentum|trend"
    )
    assert trend.samples == 2
    assert trend.win_rate == 0.5
    assert trend.mean_realized_r == 0.25
    assert report.performance_by_symbol_side[0].samples == 3


def test_backtest_quote_pnl_is_normalized_using_initial_risk():
    record = _trade("backtest-quote", source="backtest", reward=25.0)
    record["decision"] = "backtest_trade"
    record["metadata"]["reward_unit"] = "quote_pnl"
    record["metadata"]["side"] = "LONG"
    record["metadata"]["regime"] = "trend"
    record["risk_state"] = {"initial_risk_usd": 10.0}

    report = ContinuousIntelligenceEngine().analyze([record])

    assert report.eligible_trades == 1
    assert report.performance_by_strategy_regime[0].mean_realized_r == 2.5
    assert report.performance_by_strategy_regime[0].key == "momentum|trend"
    assert report.performance_by_symbol_side[0].key == "BTCUSDT|LONG"


def test_paper_quote_pnl_requires_explicit_unit_and_initial_risk():
    record = _trade("paper-quote", source="paper", reward=20.0)
    record["metadata"]["reward_unit"] = "quote_pnl"
    record["risk_state"] = {"risk_amount_usd": 10.0}
    report = ContinuousIntelligenceEngine().analyze([record])
    assert report.eligible_trades == 1
    assert report.performance_by_strategy_regime[0].mean_realized_r == 2.0

    legacy = _trade("paper-legacy", source="paper", reward=20.0)
    legacy["metadata"].pop("reward_unit")
    legacy["risk_state"] = {"risk_amount_usd": 10.0}
    rejected = ContinuousIntelligenceEngine().analyze([legacy])
    assert rejected.eligible_trades == 0
    assert rejected.rejected_experiences == 1


def test_backtest_summary_rows_are_not_trade_performance_samples():
    record = _trade("summary", source="backtest", reward=1.0)
    record["decision"] = "backtest_summary"
    report = ContinuousIntelligenceEngine().analyze([record])
    assert report.eligible_trades == 0
    assert report.rejected_experiences == 1


def test_unscaled_or_open_experiences_are_quarantined():
    unscaled = _trade("bad")
    unscaled["metadata"]["reward_unit"] = "unscaled_quote_pnl"
    open_trade = _trade("open")
    open_trade["outcome"] = None

    report = ContinuousIntelligenceEngine().analyze([unscaled, open_trade])
    assert report.eligible_trades == 0
    assert report.rejected_experiences == 2


def test_root_cause_requires_explicit_recorded_evidence():
    report = ContinuousIntelligenceEngine().analyze(
        [
            _trade("early", metadata={"premature_exit": True}),
            _trade("stale", metadata={"data_stale": True}),
            _trade("missed", metadata={"missed_opportunity_r": 0.8}),
            _trade(
                "linked-decision",
                metadata={
                    "decision_snapshot": {"root_cause_tags": ["adverse_order_flow"]}
                },
            ),
            _trade("costly", metadata={"diagnostics": {"total_cost_r": 0.4}}),
            _trade("unknown"),
        ]
    )
    causes = {item.category: item for item in report.root_causes}
    assert causes["premature_exit"].evidence_ids == ("early",)
    assert causes["data_quality"].evidence_ids == ("stale",)
    assert causes["missed_opportunity"].evidence_ids == ("missed",)
    assert causes["adverse_order_flow"].evidence_ids == ("linked-decision",)
    assert causes["execution_cost"].evidence_ids == ("costly",)
    assert "unknown" not in causes


def test_execution_cost_diagnosis_threshold_is_configurable():
    record = _trade("costly", metadata={"total_cost_r": 0.4})
    report = ContinuousIntelligenceEngine(material_cost_r_threshold=0.5).analyze(
        [record]
    )
    assert "execution_cost" not in {item.category for item in report.root_causes}
    with pytest.raises(ValueError, match="threshold"):
        ContinuousIntelligenceEngine(material_cost_r_threshold=0.0)


def test_diagnostics_generate_bounded_research_hypotheses():
    report = ContinuousIntelligenceEngine().analyze(
        [
            _trade("early-1", metadata={"premature_exit": True}),
            _trade("early-2", metadata={"premature_exit": True}),
            _trade("stale-1", metadata={"data_stale": True}),
        ]
    )

    hypotheses = ContinuousIntelligenceEngine.generate_hypotheses(
        report, minimum_samples=2
    )

    assert len(hypotheses) == 1
    assert hypotheses[0].category == "premature_exit"
    assert hypotheses[0].sample_count == 2
    assert hypotheses[0].evidence_ids == ("early-1", "early-2")
    assert hypotheses[0].approval_required is True
    assert "replay" in hypotheses[0].experiment.lower()
    with pytest.raises(ValueError, match="sample count"):
        ContinuousIntelligenceEngine.generate_hypotheses(report, minimum_samples=0)


def test_counterfactual_requires_replay_provenance_and_nets_costs():
    engine = ContinuousIntelligenceEngine()
    result = engine.analyze(
        [_trade("actual", reward=0.5)],
        counterfactual_scenarios=[
            {
                "source_type": "historical_replay",
                "scenario_id": "later-exit",
                "evidence_id": "actual",
                "actual_realized_r": 0.5,
                "alternative_gross_r": 1.4,
                "estimated_cost_r": 0.1,
                "replay_source": "clickhouse-replay:run-17",
            }
        ],
    )
    scenario = result.counterfactuals[0]
    assert scenario.alternative_net_r == pytest.approx(1.3)
    assert scenario.delta_vs_actual_r == pytest.approx(0.8)

    with pytest.raises(ValueError, match="historical_replay"):
        engine.analyze(
            [_trade("actual")],
            counterfactual_scenarios=[
                {
                    "source_type": "model_guess",
                    "scenario_id": "guess",
                    "evidence_id": "actual",
                    "actual_realized_r": 0.5,
                    "alternative_gross_r": 2.0,
                    "estimated_cost_r": 0.1,
                    "replay_source": "not-a-replay",
                }
            ],
        )


def test_counterfactual_must_match_canonical_actual_trade_result():
    record = _trade("actual", reward=0.5)
    scenario = {
        "source_type": "historical_replay",
        "scenario_id": "wrong-actual",
        "evidence_id": "actual",
        "actual_realized_r": 0.4,
        "alternative_gross_r": 1.2,
        "estimated_cost_r": 0.1,
        "replay_source": "clickhouse-replay:run-18",
    }
    with pytest.raises(ValueError, match="does not match stored outcome"):
        ContinuousIntelligenceEngine().analyze(
            [record], counterfactual_scenarios=[scenario]
        )


def test_drift_detector_fails_closed_on_small_samples_and_detects_shift():
    detector = DriftDetector(minimum_samples=10, psi_threshold=0.15)
    small = detector.evaluate({"cvd": [0, 1]}, {"cvd": [10, 11]})
    assert small.status == "insufficient_samples"

    baseline = {"cvd": [float(i) for i in range(50)]}
    current = {"cvd": [float(i + 100) for i in range(50)]}
    shifted = detector.evaluate(baseline, current)
    assert shifted.status == "drift_detected"
    assert shifted.features[0].population_stability_index > 0.15


def test_drift_detector_flags_reward_shift_from_zero_variance_baseline():
    detector = DriftDetector(minimum_samples=10)
    baseline = [1.0] * 20
    current = [0.0] * 20
    report = detector.evaluate(
        {}, {}, baseline_rewards=baseline, current_rewards=current
    )
    assert report.status == "drift_detected"
    assert report.reward_mean_z_score is None


def test_drift_detector_reports_stable_identical_distributions():
    values = [float(i % 7) for i in range(60)]
    report = DriftDetector(minimum_samples=20).evaluate(
        {"delta": values}, {"delta": values}
    )
    assert report.status == "stable"
    assert report.features[0].population_stability_index == pytest.approx(0.0)


def test_validation_digest_binds_operator_approval_to_evidence():
    class Metrics:
        total_return = 0.2
        max_drawdown = 0.1
        sharpe = 1.2
        trades = 50
        initial_equity = 10_000.0
        final_equity = 12_000.0

    class HoldoutMetrics:
        total_return = 0.1
        max_drawdown = 0.05
        sharpe = 1.4
        trades = 12
        initial_equity = 10_000.0
        final_equity = 11_000.0

    class WindowMetrics:
        total_return = 0.05
        max_drawdown = 0.03
        sharpe = 1.1
        trades = 20
        initial_equity = 10_000.0
        final_equity = 10_500.0

    class WindowResult:
        test_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        test_end = datetime(2026, 1, 31, tzinfo=timezone.utc)
        result = type("Result", (), {"metrics": WindowMetrics()})()

    class Walk:
        passed = True
        locked_holdout = type("Holdout", (), {"metrics": HoldoutMetrics()})()
        windows = [WindowResult(), WindowResult()]
        reason = "passed"

    validation = type(
        "Validation",
        (),
        {
            "passed": True,
            "reason": "passed",
            "candidate": type("Candidate", (), {"metrics": Metrics()})(),
            "walk_forward": Walk(),
        },
    )()
    digest = validation_evidence_digest(validation)
    validation.walk_forward.windows[0].result.metrics.total_return = 0.99
    assert validation_evidence_digest(validation) != digest
    validation.walk_forward.windows[0].result.metrics.total_return = 0.05
    approval = PromotionApproval(
        candidate_name="momentum",
        candidate_version="v2",
        approved_by="operator:test",
        approval_id="approval-1",
        validation_digest=digest,
        approved_at=datetime.now(timezone.utc),
    )
    approval.validate(
        candidate_name="momentum",
        candidate_version="v2",
        validation_digest=digest,
    )
    with pytest.raises(ValueError, match="does not match"):
        approval.validate(
            candidate_name="momentum",
            candidate_version="v3",
            validation_digest=digest,
        )
    with pytest.raises(ValueError, match="expired"):
        approval.validate(
            candidate_name="momentum",
            candidate_version="v2",
            validation_digest=digest,
            now=approval.approved_at + timedelta(days=2),
        )
