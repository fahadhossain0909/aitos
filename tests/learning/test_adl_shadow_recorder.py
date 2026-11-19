import pytest

from aitos.core.contracts import Event
from aitos.learning.recorder import LearningExperienceRecorder


class _Bus:
    async def subscribe(self, *args, **kwargs):
        return type("Sub", (), {"cancel": lambda self: None})()


class _Repository:
    def __init__(self):
        self.records = []

    async def ensure_learning_experience_schema(self):
        return None

    async def save_learning_experience(self, record):
        self.records.append(record)


@pytest.mark.asyncio
async def test_adl_shadow_event_is_persisted_with_execution_disabled():
    repository = _Repository()
    recorder = LearningExperienceRecorder(_Bus(), repository, "paper")
    event = Event(
        topic="decision.adl_shadow_evaluated",
        payload={
            "decision_id": "decision-1",
            "candidate_id": "candidate-1",
            "symbol": "BTCUSDT",
            "scanner_direction": "LONG",
            "adl_action": "ABSTAIN",
            "adl_proposed_action": "LONG",
            "probabilities": {"LONG": 0.7, "SHORT": 0.1, "NO_TRADE": 0.2},
            "raw_probabilities": {"LONG": 0.7, "SHORT": 0.1, "NO_TRADE": 0.2},
            "calibrated": False,
            "model_id": "native",
            "model_version": "test",
            "reasons": ["calibration cold"],
            "execution_eligible": False,
        },
        source_module="test",
    )
    await recorder._on_adl_shadow(event)
    assert len(repository.records) == 1
    record = repository.records[0]
    assert record.metadata["decision_id"] == "decision-1"
    assert record.metadata["candidate_id"] == "candidate-1"
    assert record.metadata["execution_eligible"] is False
    assert record.market_state["adl_action"] == "ABSTAIN"


@pytest.mark.asyncio
async def test_adl_shadow_event_without_identity_is_ignored():
    repository = _Repository()
    recorder = LearningExperienceRecorder(_Bus(), repository, "paper")
    event = Event(
        topic="decision.adl_shadow_evaluated",
        payload={"symbol": "BTCUSDT"},
        source_module="test",
    )
    await recorder._on_adl_shadow(event)
    assert repository.records == []
