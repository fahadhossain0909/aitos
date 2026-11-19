import hashlib

import pytest

from aitos.statistics.governance import PromotionEvidence, PromotionPolicy
from aitos.statistics.registry import StatisticalModelRegistry
from aitos.world_intelligence.model_governance import EventImpactModelGovernance


def evidence(version="2.0.0", approved=True):
    return PromotionEvidence(
        max_drawdown=0.05, drift_score=0.02,
        paired_oos_improvement_lower_bound=0.01, dsr_probability=0.99,
        mcs_included=True, reality_check_p_value=0.01, dm_p_value=0.01,
        multiple_testing_adjusted_p_value=0.02, operator_approved=approved,
    )


def register(governance, version):
    digest = hashlib.sha256(version.encode()).hexdigest()
    from aitos.world_intelligence.model_governance import EventImpactCandidate
    return governance.register_challenger(EventImpactCandidate(
        model_id="event-impact", model_version=version,
        artifact_uri=f"artifacts/{version}", artifact_sha256=digest,
        config_hash=digest, feature_schema_version="event-v1",
        metadata={"validation": "walk-forward"},
    ))


def test_drift_blocks_promotion_and_operator_approval_is_mandatory(tmp_path):
    registry = StatisticalModelRegistry(tmp_path / "models.sqlite")
    governance = EventImpactModelGovernance(
        registry, policy=PromotionPolicy(min_observations=100, min_shadow_days=1)
    )
    register(governance, "2.0.0")
    ref = {"sentiment": [float(i) for i in range(100)]}
    shifted = {"sentiment": [float(i + 1000) for i in range(100)]}
    assert governance.observe_drift(ref, shifted).promotion_blocked
    blocked = governance.promote(evidence(), operator_approved=True)
    assert not blocked.eligible
    assert registry.get_champion("event-impact") is None


def test_promotion_and_rollback_require_explicit_approval(tmp_path):
    registry = StatisticalModelRegistry(tmp_path / "models.sqlite")
    governance = EventImpactModelGovernance(
        registry, policy=PromotionPolicy(min_observations=100, min_shadow_days=1)
    )
    register(governance, "1.0.0")
    register(governance, "2.0.0")
    reference = {"sentiment": [float(i) for i in range(100)]}
    governance.observe_drift(reference, reference)
    assert not governance.promote(evidence("1.0.0"), operator_approved=False).eligible
    assert governance.promote(evidence("1.0.0"), operator_approved=True).eligible
    assert registry.get_champion("event-impact").model_version == "1.0.0"
    assert governance.promote(evidence("2.0.0"), operator_approved=True).eligible
    assert registry.get_champion("event-impact").model_version == "2.0.0"
    with pytest.raises(PermissionError, match="approval"):
        governance.rollback("event-impact", "1.0.0")
    restored = governance.rollback("event-impact", "1.0.0", operator_approved=True)
    assert restored.model_version == "1.0.0"
