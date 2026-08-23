from datetime import datetime, timezone

from aitos.learning.evolution import EvolutionEngine, EvolutionProposal
from aitos.learning.experience import ExperienceRecord
from aitos.learning.model_registry import ModelArtifact, ModelRegistry


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


def test_registry_promotes_candidate_and_archives_previous(tmp_path):
    registry = ModelRegistry(str(tmp_path / "registry.json"))
    registry.register(ModelArtifact("strategy", "v1", "parameter", status="champion"))
    registry.register(ModelArtifact("strategy", "v2", "parameter", status="candidate"))
    promoted = registry.promote("strategy", "v2")

    assert promoted.status == "champion"
    assert registry.get_champion("strategy").version == "v2"
