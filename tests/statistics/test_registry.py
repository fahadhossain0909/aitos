import hashlib

import pytest

from aitos.statistics.governance import PromotionEvidence
from aitos.statistics.registry import StatisticalModelRegistry


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def register(registry, version, artifact_content):
    return registry.register_candidate(
        model_id="a-stat",
        model_version=version,
        artifact_uri=f"models/a-stat/{version}.bin",
        artifact_sha256=digest(artifact_content),
        config_hash=digest("config-v1"),
        feature_schema_version="1",
        metadata={"training_run": f"run-{version}"},
    )


def evidence(version, *, approved=True, improvement=0.01):
    return PromotionEvidence(
        model_id="a-stat",
        model_version=version,
        observations=2000,
        shadow_days=45,
        brier=0.18,
        qlike=1.2,
        pbo=0.08,
        max_drawdown=0.05,
        drift_score=0.02,
        paired_oos_improvement_lower_bound=improvement,
        dsr_probability=0.99,
        mcs_included=True,
        reality_check_p_value=0.01,
        dm_p_value=0.01,
        multiple_testing_adjusted_p_value=0.02,
        operator_approved=approved,
    )


def test_registry_registration_is_immutable_and_idempotent(tmp_path):
    registry = StatisticalModelRegistry(tmp_path / "registry.sqlite3")
    assert register(registry, "1.0.0", "artifact-one")
    assert not register(registry, "1.0.0", "artifact-one")
    with pytest.raises(ValueError, match="already registered differently"):
        register(registry, "1.0.0", "different-artifact")
    artifact = registry.list_artifacts("a-stat")[0]
    assert artifact.status == "CHALLENGER"
    assert artifact.metadata["training_run"] == "run-1.0.0"


def test_registry_requires_evidence_and_operator_approval_for_promotion(tmp_path):
    registry = StatisticalModelRegistry(tmp_path / "registry.sqlite3")
    register(registry, "1.0.0", "artifact-one")
    blocked = registry.promote(evidence("1.0.0", approved=False))
    assert not blocked.eligible
    assert "explicit_operator_approval_required" in blocked.reasons
    assert registry.get_champion("a-stat") is None
    history = registry.promotion_history("a-stat")
    assert history[-1]["action"] == "PROMOTION_BLOCKED"
    assert "explicit_operator_approval_required" in history[-1]["decision"]["reasons"]

    promoted = registry.promote(evidence("1.0.0"))
    assert promoted.eligible
    assert registry.promotion_history("a-stat")[-1]["action"] == "PROMOTED"
    champion = registry.get_champion("a-stat")
    assert champion is not None
    assert champion.model_version == "1.0.0"
    assert champion.status == "CHAMPION"


def test_registry_champion_replacement_and_manual_rollback(tmp_path):
    registry = StatisticalModelRegistry(tmp_path / "registry.sqlite3")
    register(registry, "1.0.0", "artifact-one")
    register(registry, "2.0.0", "artifact-two")
    registry.promote(evidence("1.0.0"))
    registry.promote(evidence("2.0.0"))
    assert registry.get_champion("a-stat").model_version == "2.0.0"
    versions = {
        item.model_version: item.status for item in registry.list_artifacts("a-stat")
    }
    assert versions == {"1.0.0": "ARCHIVED", "2.0.0": "CHAMPION"}

    with pytest.raises(PermissionError):
        registry.rollback("a-stat", "1.0.0")
    assert registry.promotion_history("a-stat")[-1]["action"] == "ROLLBACK_BLOCKED"
    restored = registry.rollback("a-stat", "1.0.0", operator_approved=True)
    assert restored.model_version == "1.0.0"
    assert registry.promotion_history("a-stat")[-1]["action"] == "ROLLBACK"
    assert registry.get_champion("a-stat").model_version == "1.0.0"


def test_registry_rejects_unregistered_or_invalid_artifacts(tmp_path):
    registry = StatisticalModelRegistry(tmp_path / "registry.sqlite3")
    with pytest.raises(KeyError):
        registry.promote(evidence("missing"))
    with pytest.raises(ValueError, match="SHA-256"):
        registry.register_candidate(
            model_id="a-stat",
            model_version="bad",
            artifact_uri="models/bad.bin",
            artifact_sha256="not-a-hash",
            config_hash=digest("config"),
            feature_schema_version="1",
        )
