import pytest

from aitos.core.contracts import Event
from aitos.learning.recorder import LearningExperienceRecorder


class FakeRepository:
    def __init__(self):
        self.records = []

    async def save_learning_experience(self, record):
        self.records.append(record)


@pytest.mark.asyncio
async def test_outcome_record_retains_entry_features_and_mae_mfe():
    repository = FakeRepository()
    recorder = LearningExperienceRecorder(
        event_bus=None, repository=repository, source="paper"
    )
    event = Event(
        topic="journal.outcome_attributed",
        payload={
            "decision_id": "decision-1",
            "trade_id": "trade-1",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "pnl": 12.5,
            "pnl_percent": 1.25,
            "entry_price": 100.0,
            "exit_price": 102.0,
            "exit_reason": "take_profit",
            "regime": "trending",
            "mae_r": 0.2,
            "mfe_r": 1.5,
            "agent_consensus": {"order_flow_bias": 8.0, "entry_timing": 7.0},
        },
        source_module="journal-system",
    )

    await recorder._on_outcome(event)

    record = repository.records[0]
    assert record.features == {"order_flow_bias": 8.0, "entry_timing": 7.0}
    assert record.metadata["mae_r"] == 0.2
    assert record.metadata["mfe_r"] == 1.5
    assert record.price == 100.0
    assert record.reward == 12.5

@pytest.mark.asyncio
async def test_paper_live_outcome_uses_r_multiple_and_records_premature_exit_feedback():
    repository = FakeRepository()
    recorder = LearningExperienceRecorder(
        event_bus=None, repository=repository, source="live"
    )
    event = Event(
        topic="journal.outcome_attributed",
        payload={
            "trade_id": "trade-feedback",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "pnl": 4.0,
            "risk_amount_usd": 5.0,
            "entry_price": 100.0,
            "exit_price": 100.8,
            "exit_reason": "manual_exit",
            "strategy_id": "mean_reversion",
            "regime": "trending",
            "mae_r": 0.2,
            "mfe_r": 3.0,
        },
        source_module="journal-system",
    )

    await recorder._on_outcome(event)

    record = repository.records[0]
    assert record.reward == pytest.approx(0.8)
    assert record.risk_state == {"risk_amount_usd": 5.0}
    assert record.strategy_version == "mean_reversion"
    assert record.metadata["reward_unit"] == "r_multiple"
    assert record.metadata["premature_exit"] is True
    assert record.metadata["missed_opportunity_r"] == pytest.approx(2.2)
    assert record.metadata["realized_r"] == pytest.approx(0.8)


def test_unscaled_paper_outcome_is_marked_so_worker_can_reject_it():
    repository = FakeRepository()
    recorder = LearningExperienceRecorder(
        event_bus=None, repository=repository, source="paper"
    )
    event = Event(
        topic="journal.outcome_attributed",
        payload={"trade_id": "no-risk", "symbol": "BTCUSDT", "pnl": 5.0},
        source_module="journal-system",
    )

    import asyncio

    asyncio.run(recorder._on_outcome(event))
    record = repository.records[0]
    assert record.reward == 5.0
    assert record.metadata["reward_unit"] == "unscaled_quote_pnl"
    assert record.risk_state == {}
