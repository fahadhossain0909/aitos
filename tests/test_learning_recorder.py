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
            "decision_snapshot": {
                "root_cause_tags": ["adverse_order_flow"],
                "candidate_score": 8.2,
            },
            "root_cause_tags": ["adverse_order_flow"],
            "total_cost_r": 0.3,
            "diagnostics": {"execution_degraded": True},
        },
        source_module="journal-system",
    )

    await recorder._on_outcome(event)

    record = repository.records[0]
    assert record.features == {"order_flow_bias": 8.0, "entry_timing": 7.0}
    assert record.metadata["mae_r"] == 0.2
    assert record.metadata["mfe_r"] == 1.5
    assert record.metadata["decision_snapshot"]["root_cause_tags"] == [
        "adverse_order_flow"
    ]
    assert record.metadata["root_cause_tags"] == ["adverse_order_flow"]
    assert record.metadata["total_cost_r"] == 0.3
    assert record.metadata["diagnostics"]["execution_degraded"] is True
    assert record.price == 100.0
    assert record.reward == 12.5


@pytest.mark.asyncio
async def test_decision_record_retains_scanner_attribution_context():
    repository = FakeRepository()
    recorder = LearningExperienceRecorder(
        event_bus=None, repository=repository, source="paper"
    )
    event = Event(
        topic="journal.decision_recorded",
        payload={
            "decision_id": "decision-diagnostics",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "confidence": 0.8,
            "candidate_score": 8.1,
            "candidate_component_scores": {"cvd": 8.5, "htf": 7.0},
            "candidate_component_availability": {"cvd": True, "htf": True},
            "candidate_weights": {"cvd": 0.6, "htf": 0.4},
            "candidate_threshold": 6.5,
            "candidate_rationale": ["aligned_order_flow", "htf_confirmed"],
        },
        source_module="journal-system",
    )

    await recorder._on_decision(event)

    diagnostics = repository.records[0].metadata["decision_diagnostics"]
    assert diagnostics["candidate_score"] == 8.1
    assert diagnostics["component_scores"] == {"cvd": 8.5, "htf": 7.0}
    assert diagnostics["component_availability"]["htf"] is True
    assert diagnostics["weights"] == {"cvd": 0.6, "htf": 0.4}
    assert diagnostics["threshold"] == 6.5
    assert diagnostics["rationale"] == ["aligned_order_flow", "htf_confirmed"]


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


@pytest.mark.asyncio
async def test_recorder_can_be_restarted_without_duplicate_subscriptions():
    class Subscription:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    class FakeBus:
        def __init__(self):
            self.subscriptions = []

        async def subscribe(self, *args, **kwargs):
            subscription = Subscription()
            self.subscriptions.append(subscription)
            return subscription

    class SchemaRepository(FakeRepository):
        async def ensure_learning_experience_schema(self):
            return None

    bus = FakeBus()
    recorder = LearningExperienceRecorder(
        event_bus=bus, repository=SchemaRepository(), source="paper"
    )

    await recorder.initialize({})
    first_pair = list(bus.subscriptions)
    assert len(first_pair) == 3

    await recorder.shutdown()
    assert all(subscription.cancelled for subscription in first_pair)
    assert (await recorder.health_check()).status.value == "unhealthy"

    await recorder.initialize({})
    assert len(bus.subscriptions) == 6
    assert all(subscription.cancelled for subscription in first_pair)
    assert (await recorder.health_check()).status.value == "healthy"

    await recorder.shutdown()


@pytest.mark.asyncio
async def test_management_and_rejected_outcomes_are_recorded_without_reward_leakage():
    from aitos.core.contracts import Event

    repository = FakeRepository()
    recorder = LearningExperienceRecorder(
        event_bus=None, repository=repository, source="paper"
    )
    await recorder._on_management_decision(
        Event(
            topic="journal.management_decision_recorded",
            payload={
                "decision_id": "d:hold-1",
                "trade_id": "t-1",
                "symbol": "BTCUSDT",
                "action": "HOLD",
            },
            source_module="journal",
        )
    )
    await recorder._on_outcome(
        Event(
            topic="journal.outcome_attributed",
            payload={
                "decision_id": "d-2",
                "trade_id": "t-2",
                "symbol": "ETHUSDT",
                "side": "LONG",
                "state": "rejected",
                "execution_failure": True,
                "rejection_reason": "order submission failed",
            },
            source_module="journal",
        )
    )
    assert repository.records[0].metadata["event_type"] == "management_decision"
    assert repository.records[0].outcome is None
    assert repository.records[1].outcome == "rejected"
    assert repository.records[1].reward == 0.0
    assert repository.records[1].metadata["reward_unit"] == "not_applicable"


@pytest.mark.asyncio
async def test_duplicate_outcome_delivery_is_idempotent_across_recorder_restarts():
    event = Event(
        topic="journal.outcome_attributed",
        payload={
            "trade_id": "stable-trade-1",
            "decision_id": "stable-decision-1",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "state": "closed",
            "pnl": 5.0,
            "risk_amount_usd": 10.0,
        },
        source_module="journal",
    )
    first_repository = FakeRepository()
    first_recorder = LearningExperienceRecorder(
        event_bus=None, repository=first_repository, source="paper"
    )
    await first_recorder._on_outcome(event)
    await first_recorder._on_outcome(event)
    assert len(first_repository.records) == 1

    restarted_repository = FakeRepository()
    restarted_recorder = LearningExperienceRecorder(
        event_bus=None, repository=restarted_repository, source="paper"
    )
    await restarted_recorder._on_outcome(event)
    assert (
        restarted_repository.records[0].experience_id
        == first_repository.records[0].experience_id
    )
