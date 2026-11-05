from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from aitos.learning.evolution import EvolutionEngine, EvolutionProposal
from aitos.learning.experience import ExperienceRecord
from aitos.learning.model_registry import ModelArtifact, ModelRegistry
from aitos.learning.regime_feedback import ModelComparison


def test_experience_record_is_stage_bound_and_serializable():
    record = ExperienceRecord(
        timestamp=datetime.now(timezone.utc),
        source="backtest",
        symbol="BTCUSDT",
        decision="long",
        confidence=0.8,
        reward=1.2,
    )
    payload = record.as_dict()
    assert payload["source"] == "backtest"
    assert payload["symbol"] == "BTCUSDT"
    assert payload["reward"] == 1.2


def test_evolution_proposal_is_bounded():
    proposal = EvolutionProposal(
        model_name="strategy",
        parent_version="v1",
        change_type="parameter",
        change={"target_version": "v2", "cvd_threshold": 0.78},
        rationale="Reduce false entries in low-liquidity regimes",
    )
    EvolutionEngine.validate_proposal(proposal)


def _promotion_validation(*, champion=None):
    candidate_metrics = SimpleNamespace(
        total_return=0.2,
        max_drawdown=0.08,
        sharpe=1.5,
        trades=40,
        initial_equity=10_000.0,
        final_equity=12_000.0,
    )
    holdout_metrics = SimpleNamespace(
        total_return=0.08,
        max_drawdown=0.04,
        sharpe=1.7,
        trades=12,
        initial_equity=10_000.0,
        final_equity=10_800.0,
    )
    walk_forward = SimpleNamespace(
        passed=True,
        locked_holdout=SimpleNamespace(metrics=holdout_metrics),
        windows=[object(), object()],
        reason="walk-forward and locked holdout passed",
    )
    return SimpleNamespace(
        passed=True,
        reason="candidate passed validation",
        candidate=SimpleNamespace(metrics=candidate_metrics),
        champion=champion,
        walk_forward=walk_forward,
    )


def test_registry_requires_validated_candidate_and_statistical_challenger_gate(
    tmp_path,
):
    registry = ModelRegistry(str(tmp_path / "registry.json"))
    registry.register(ModelArtifact("strategy", "v1", "parameter"))
    first = registry.promote(
        "strategy", "v1", validation_result=_promotion_validation()
    )
    assert first.status == "champion"
    assert (
        first.metadata["promotion_evidence"]["locked_holdout_metrics"]["total_return"]
        == 0.08
    )

    registry.register(ModelArtifact("strategy", "v2", "parameter"))
    validation = _promotion_validation(champion=SimpleNamespace(metrics=object()))
    with pytest.raises(ValueError, match="statistical comparison"):
        registry.promote("strategy", "v2", validation_result=validation)

    comparison = ModelComparison(
        accepted=True,
        candidate_mean=1.2,
        champion_mean=0.8,
        standard_error=0.1,
        reason="candidate clears confidence-bound improvement gate",
    )
    promoted = registry.promote(
        "strategy",
        "v2",
        validation_result=validation,
        champion_comparison=comparison,
    )

    assert promoted.status == "champion"
    assert registry.get_champion("strategy").version == "v2"
    assert promoted.metadata["promotion_evidence"]["champion_comparison"]["accepted"]


def test_registry_refuses_unvalidated_or_non_candidate_models(tmp_path):
    registry = ModelRegistry(str(tmp_path / "registry.json"))
    with pytest.raises(ValueError, match="registered as candidates"):
        registry.register(
            ModelArtifact("strategy", "v1", "parameter", status="champion")
        )
    registry.register(ModelArtifact("strategy", "v2", "parameter"))
    with pytest.raises(ValueError, match="failed validation"):
        registry.promote(
            "strategy",
            "v2",
            validation_result=SimpleNamespace(passed=False),
        )
    with pytest.raises(ValueError, match="walk-forward and locked holdout"):
        registry.promote(
            "strategy",
            "v2",
            validation_result=SimpleNamespace(
                passed=True,
                reason="not enough",
                candidate=SimpleNamespace(metrics=SimpleNamespace()),
                walk_forward=None,
            ),
        )


def test_model_registry_serializes_concurrent_candidate_writes(tmp_path):
    path = tmp_path / "registry.json"
    registries = [ModelRegistry(str(path)) for _ in range(8)]

    def register(index):
        registries[index % len(registries)].register(
            ModelArtifact(
                "strategy",
                f"v{index}",
                "parameter",
                metadata={"writer": index},
            )
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(register, range(40)))

    rows = json.loads(path.read_text(encoding="utf-8"))
    assert len(rows) == 40
    assert {row["version"] for row in rows} == {f"v{i}" for i in range(40)}
