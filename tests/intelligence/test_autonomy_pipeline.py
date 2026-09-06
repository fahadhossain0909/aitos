from aitos.intelligence.autonomy_pipeline import (
    AutonomyPipeline,
    EvidenceItem,
    FailClosedPolicy,
    KnowledgeSnapshot,
    LearningOutcome,
)


def knowledge(*evidence):
    return KnowledgeSnapshot(
        market_state="trend:expansion",
        regime="trend",
        evidence=tuple(evidence),
        features={"volatility": 0.4},
    )


def test_pipeline_is_auditable_and_replayable():
    pipeline = AutonomyPipeline(policy_version="v1")
    record = pipeline.decide(
        knowledge(
            EvidenceItem("trend", 0.9, 1.0, 1.0),
            EvidenceItem("flow", 0.8, 0.9, 1.0),
        ),
        instrument="BTCUSDT",
        quantity=1.0,
    )
    assert record.intent.action == "long"
    assert record.policy.allowed is True
    replay = pipeline.replay(record)
    assert replay["decision"]["decision_id"] == record.decision.decision_id
    assert replay["knowledge"]["evidence_hash"] == record.knowledge.evidence_hash


def test_policy_fails_closed_on_stale_evidence():
    pipeline = AutonomyPipeline(
        policy=FailClosedPolicy(max_staleness_seconds=5.0),
        policy_version="v1",
    )
    record = pipeline.decide(
        knowledge(EvidenceItem("flow", 0.9, 1.0, 10.0)),
        instrument="BTCUSDT",
        quantity=1.0,
    )
    assert record.policy.allowed is False
    assert record.intent.action == "no_trade"
    assert record.intent.quantity == 0.0
    assert "stale evidence" in record.policy.reasons


def test_risk_veto_cannot_be_overridden_by_decision():
    pipeline = AutonomyPipeline(policy_version="v1")
    record = pipeline.decide(
        knowledge(EvidenceItem("flow", 0.95, 1.0, 0.0)),
        instrument="BTCUSDT",
        quantity=1.0,
        risk_approved=False,
    )
    assert record.decision.action == "long"
    assert record.intent.action == "no_trade"
    assert record.policy.allowed is False


def test_learning_requires_known_decision_and_does_not_mutate_policy():
    pipeline = AutonomyPipeline(policy_version="v1")
    record = pipeline.decide(
        knowledge(EvidenceItem("flow", 0.8, 1.0, 0.0)), instrument="BTCUSDT"
    )
    pipeline.learn(
        LearningOutcome(record.decision.decision_id, "long", 12.5, True, "trend")
    )
    assert pipeline.policy_version == "v1"
    assert pipeline.snapshot()["outcomes"] == 1
