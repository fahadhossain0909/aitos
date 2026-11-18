"""ADL parser, calibration, aggregation and fail-closed runtime tests."""

from datetime import datetime, timedelta, timezone

import pytest

from aitos.decision import (
    ADLRuntime,
    AgentAssessment,
    DecisionAction,
    DecisionLanguageError,
    DecisionPolicy,
    InMemoryDecisionAttributionStore,
    MarketSnapshot,
    ModelResponse,
    ProbabilityCalibrator,
    TradeEconomics,
    parse_decision_spec,
)
from aitos.decision.attribution import DecisionOutcome

SPEC_SOURCE = """
DECISION pretrade:
STATE:
  - trend
  - order_flow
  - risk_veto
ASK direction: CHOICE [LONG, SHORT, NO_TRADE]
ASK quality: SCORE 1..5
ASK reversal: NOUL "liquidity sweep is a genuine reversal"
OUTPUT:
  - decision
  - probabilities
  - reasons
"""


class StaticProvider:
    def __init__(self, result):
        self.result = result

    def evaluate(self, snapshot, spec):
        return self.result


def snapshot(**features):
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    return MarketSnapshot(
        "BTCUSDT",
        now,
        {"trend": 0.8, "order_flow": 0.7, "risk_veto": False, **features},
        snapshot_id="snapshot-1",
    )


def response(**overrides):
    values = dict(
        probabilities={"LONG": 0.75, "SHORT": 0.10, "NO_TRADE": 0.15},
        economics=TradeEconomics(0.65, 100.0, 100.0, 5.0),
        model_id="native-test",
        model_version="1",
    )
    values.update(overrides)
    return ModelResponse(**values)


@pytest.fixture
def spec():
    return parse_decision_spec(SPEC_SOURCE)


def test_parser_builds_typed_spec(spec):
    assert spec.name == "pretrade"
    assert spec.state_fields == ("trend", "order_flow", "risk_veto")
    assert spec.questions[0].options == ("LONG", "SHORT", "NO_TRADE")
    assert spec.questions[1].minimum == 1
    assert spec.questions[2].statement.startswith("liquidity sweep")


@pytest.mark.parametrize(
    "source",
    [
        "",
        "STATE:\n - trend",
        "DECISION x:\nRUN arbitrary_python()",
        "DECISION x:\nASK flag: NOUL unquoted statement",
        "DECISION x:\nASK side: CHOICE [LONG, LONG]",
    ],
)
def test_parser_rejects_invalid_or_executable_syntax(source):
    with pytest.raises((DecisionLanguageError, ValueError)):
        parse_decision_spec(source)


def test_runtime_is_advisory_only_in_shadow_mode(spec):
    runtime = ADLRuntime(
        StaticProvider(response()),
        policy=DecisionPolicy(require_calibration=False, shadow_mode=True),
    )
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    result = runtime.evaluate(snapshot(), spec, now=now)
    assert result.action == DecisionAction.LONG
    assert result.proposed_action == DecisionAction.LONG
    assert result.execution_eligible is False
    assert result.expected_value == pytest.approx(25.0)


def test_runtime_abstains_when_calibration_is_cold(spec):
    runtime = ADLRuntime(StaticProvider(response()))
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    result = runtime.evaluate(snapshot(), spec, now=now)
    assert result.action == DecisionAction.ABSTAIN
    assert "calibration" in " ".join(result.reasons)


def test_runtime_abstains_on_naive_evaluation_clock(spec):
    runtime = ADLRuntime(
        StaticProvider(response()), policy=DecisionPolicy(require_calibration=False)
    )
    naive_now = datetime(2026, 11, 18, 12, 0)
    result = runtime.evaluate(snapshot(), spec, now=naive_now)
    assert result.action == DecisionAction.ABSTAIN
    assert result.evaluated_at.tzinfo is not None


def test_runtime_fails_closed_on_stale_or_missing_state(spec):
    runtime = ADLRuntime(
        StaticProvider(response()), policy=DecisionPolicy(require_calibration=False)
    )
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    stale = MarketSnapshot(
        "BTCUSDT",
        now - timedelta(seconds=30),
        {"trend": 1, "order_flow": 1, "risk_veto": False},
    )
    assert runtime.evaluate(stale, spec, now=now).action == DecisionAction.ABSTAIN
    missing = MarketSnapshot("BTCUSDT", now, {"trend": 1})
    assert runtime.evaluate(missing, spec, now=now).action == DecisionAction.ABSTAIN


def test_future_snapshot_is_rejected(spec):
    runtime = ADLRuntime(
        StaticProvider(response()), policy=DecisionPolicy(require_calibration=False)
    )
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    assert (
        runtime.evaluate(snapshot(), spec, now=now - timedelta(seconds=1)).action
        == DecisionAction.ABSTAIN
    )


def test_hard_risk_veto_cannot_be_overridden(spec):
    runtime = ADLRuntime(
        StaticProvider(response()), policy=DecisionPolicy(require_calibration=False)
    )
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    result = runtime.evaluate(snapshot(risk_veto=True), spec, now=now)
    assert result.action == DecisionAction.NO_TRADE
    assert result.model_id == "unavailable"


def test_invalid_probabilities_abstain(spec):
    bad = response(probabilities={"LONG": float("nan"), "SHORT": 0.1, "NO_TRADE": 0.9})
    runtime = ADLRuntime(
        StaticProvider(bad), policy=DecisionPolicy(require_calibration=False)
    )
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    assert runtime.evaluate(snapshot(), spec, now=now).action == DecisionAction.ABSTAIN


def test_conflicting_agents_cause_no_trade(spec):
    assessments = (
        AgentAssessment("structure", {"LONG": 0.9, "SHORT": 0.05, "NO_TRADE": 0.05}),
        AgentAssessment("flow", {"LONG": 0.1, "SHORT": 0.8, "NO_TRADE": 0.1}),
    )
    runtime = ADLRuntime(
        StaticProvider(response(assessments=assessments)),
        policy=DecisionPolicy(require_calibration=False, maximum_disagreement=0.4),
    )
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    result = runtime.evaluate(snapshot(), spec, now=now)
    assert result.action == DecisionAction.NO_TRADE
    assert result.evidence_disagreement > 0.4


def test_correlated_agents_share_group_weight_budget():
    from aitos.decision import aggregate_assessments

    assessments = (
        AgentAssessment(
            "a",
            {"LONG": 0.9, "SHORT": 0.05, "NO_TRADE": 0.05},
            correlation_group="shared",
        ),
        AgentAssessment(
            "b",
            {"LONG": 0.9, "SHORT": 0.05, "NO_TRADE": 0.05},
            correlation_group="shared",
        ),
        AgentAssessment("c", {"LONG": 0.1, "SHORT": 0.8, "NO_TRADE": 0.1}),
    )
    result = aggregate_assessments(assessments)
    assert result.effective_agent_count < 3
    assert sum(result.probabilities.values()) == pytest.approx(1.0)


def test_calibrator_requires_samples_and_reports_metrics():
    calibrator = ProbabilityCalibrator(bins=5, minimum_samples=2)
    assert calibrator.calibrate("LONG", 0.7) == (0.7, False)
    calibrator.update("LONG", 0.7, True)
    calibrator.update("LONG", 0.72, False)
    value, ready = calibrator.calibrate("LONG", 0.71)
    assert ready and 0 <= value <= 1
    assert calibrator.metrics("LONG").samples == 2
    assert calibrator.metrics("LONG").brier_score is not None


def test_attribution_store_is_idempotent_and_requires_known_decision(spec):
    runtime = ADLRuntime(
        StaticProvider(response()), policy=DecisionPolicy(require_calibration=False)
    )
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    result = runtime.evaluate(snapshot(), spec, now=now)
    store = InMemoryDecisionAttributionStore()
    store.record_decision(result)
    store.record_decision(result)
    outcome = DecisionOutcome(
        result.decision_id,
        "WIN",
        20.0,
        1.0,
        0.2,
        0.1,
        -3.0,
        25.0,
        datetime(2026, 11, 18, 13, 0, tzinfo=timezone.utc),
    )
    store.record_outcome(outcome)
    store.record_outcome(outcome)
    assert store.get_outcome(result.decision_id) == outcome


def test_negative_expected_value_blocks_trade_candidate(spec):
    economics = TradeEconomics(0.2, 100.0, 100.0, 10.0)
    runtime = ADLRuntime(
        StaticProvider(response(economics=economics)),
        policy=DecisionPolicy(require_calibration=False),
    )
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    result = runtime.evaluate(snapshot(), spec, now=now)
    assert result.action == DecisionAction.NO_TRADE
    assert result.expected_value < 0
