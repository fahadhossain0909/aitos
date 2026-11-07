import pytest

from aitos.statistics.governance import (
    PromotionEvidence,
    PromotionPolicy,
    evaluate_promotion,
)


def valid_evidence(**overrides):
    values = {
        "model_id": "a-stat",
        "model_version": "2.0.0",
        "observations": 2000,
        "shadow_days": 45,
        "brier": 0.18,
        "qlike": 1.2,
        "pbo": 0.08,
        "max_drawdown": 0.05,
        "drift_score": 0.02,
        "paired_oos_improvement_lower_bound": 0.01,
        "operator_approved": True,
    }
    values.update(overrides)
    return PromotionEvidence(**values)


def test_promotion_requires_all_evidence_and_explicit_approval():
    decision = evaluate_promotion(valid_evidence())
    assert decision.eligible
    assert not decision.reasons


def test_promotion_fails_closed_for_sample_drift_and_approval_gaps():
    decision = evaluate_promotion(
        valid_evidence(
            observations=100,
            shadow_days=2,
            pbo=0.8,
            drift_score=0.5,
            operator_approved=False,
            paired_oos_improvement_lower_bound=-0.01,
        )
    )
    assert not decision.eligible
    assert "insufficient_out_of_sample_observations" in decision.reasons
    assert "explicit_operator_approval_required" in decision.reasons
    assert "out_of_sample_improvement_not_statistically_positive" in decision.reasons


def test_promotion_policy_and_evidence_reject_invalid_metrics():
    with pytest.raises(ValueError):
        PromotionPolicy(min_observations=0)
    with pytest.raises(ValueError):
        evaluate_promotion(valid_evidence(pbo=1.1))
    with pytest.raises(ValueError):
        evaluate_promotion(valid_evidence(brier=float("nan")))
