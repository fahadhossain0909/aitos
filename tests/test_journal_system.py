import asyncio

import pytest

from aitos.journal.journal_system import JournalSystem
from aitos.journal.models import JournalEntryType
from aitos.models.trade import Opportunity, TradeSide
from aitos.risk.models import PortfolioState
from aitos.trading.lifecycle import TradeLifecycle


def make_opportunity(**overrides):
    defaults = dict(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        stop_loss_price=98.0,
        take_profit_levels=[104.0],
        confidence=0.8,
        strategy_id="test-strategy",
        rationale="test rationale",
        agent_consensus={
            "trend_strength": 8.0,
            "order_flow_bias": 7.0,
            "liquidity_quality": 2.0,
        },
    )
    defaults.update(overrides)
    return Opportunity(**defaults)


def make_portfolio(**overrides):
    defaults = dict(equity_usd=10_000.0, peak_equity_usd=10_000.0)
    defaults.update(overrides)
    return PortfolioState(**defaults)


async def _wait_for(predicate, timeout=3.0, interval=0.1):
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return False


@pytest.mark.asyncio
async def test_journal_auto_records_pre_trade_entry_on_position_opened(
    event_bus, risk_engine
):
    journal = JournalSystem(event_bus=event_bus, risk_engine=risk_engine)
    await journal.initialize({})
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})

    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())

    # The capital gate must authorize a valid legacy lifecycle opportunity so
    # journal publication can observe the opened-position event.
    assert trade.state.value == "position_opened"
    assert trade.agent_consensus["capital_objective"]["eligible"] is True
    assert await _wait_for(lambda: journal.get_explanation(trade.trade_id) is not None)

    explanation = journal.get_explanation(trade.trade_id)
    assert "LONG BTCUSDT" in explanation.why_trade
    assert any(
        e.entry_type == JournalEntryType.PRE_TRADE for e in journal.get_entries()
    )


@pytest.mark.asyncio
async def test_journal_auto_records_post_trade_entry_on_position_closed(
    event_bus, risk_engine
):
    journal = JournalSystem(event_bus=event_bus, risk_engine=risk_engine)
    await journal.initialize({})
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})

    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    await lifecycle.update_price(trade.trade_id, 105.0)  # hits TP, closes

    def has_post_trade():
        return any(
            e.entry_type == JournalEntryType.POST_TRADE for e in journal.get_entries()
        )

    assert await _wait_for(has_post_trade)
    post_entry = next(
        e for e in journal.get_entries() if e.entry_type == JournalEntryType.POST_TRADE
    )
    assert post_entry.market_context["exit_reason"] == "tp_triggered"
    assert post_entry.market_context["pnl"] > 0


@pytest.mark.asyncio
async def test_journal_records_rejected_opportunities(event_bus, risk_engine):
    await risk_engine.trigger_emergency_stop("test trip")
    journal = JournalSystem(event_bus=event_bus, risk_engine=risk_engine)
    await journal.initialize({})
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})

    await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())

    def has_rejection():
        return any(e.market_context.get("rejected") for e in journal.get_entries())

    assert await _wait_for(has_rejection)


@pytest.mark.asyncio
async def test_journal_links_decision_snapshot_to_closed_trade_outcome(
    event_bus, risk_engine
):
    journal = JournalSystem(event_bus=event_bus, risk_engine=risk_engine)
    await journal.initialize({})
    received = []

    async def capture(event):
        received.append(event)

    await event_bus.subscribe(
        "journal.outcome_attributed", capture, group="linked-decision-outcome-test"
    )
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    trade.agent_consensus["decision_id"] = "decision-linked"
    journal._decision_snapshots["decision-linked"] = {
        "decision_id": "decision-linked",
        "root_cause_tags": ["adverse_order_flow"],
        "candidate_score": 7.8,
    }

    await lifecycle.update_price(trade.trade_id, 105.0)

    assert await _wait_for(lambda: len(received) == 1)
    assert received[0].payload["decision_id"] == "decision-linked"
    assert received[0].payload["decision_snapshot"]["root_cause_tags"] == [
        "adverse_order_flow"
    ]


@pytest.mark.asyncio
async def test_record_mistake_creates_mistake_entry(event_bus, risk_engine):
    journal = JournalSystem(event_bus=event_bus)
    await journal.initialize({})

    entry = await journal.record_mistake(
        "trade-123",
        "Entered too early before confirmation",
        lesson="Wait for structure confirmation",
        improvement="Add confirmation candle rule",
    )

    assert entry.entry_type == JournalEntryType.MISTAKE
    assert entry.mistakes == ["Entered too early before confirmation"]
    assert entry.lessons == ["Wait for structure confirmation"]
    assert entry in journal.get_entries()


@pytest.mark.asyncio
async def test_generate_daily_review_persists_and_publishes(event_bus):
    from datetime import datetime, timezone

    from aitos.models.trade import Trade, TradeLifecycleState

    journal = JournalSystem(event_bus=event_bus)
    await journal.initialize({})

    received = []

    async def handler(event):
        received.append(event)

    await event_bus.subscribe("journal.daily_review", handler, group="test")

    now = datetime.now(timezone.utc).isoformat()
    trades = [
        Trade(
            trade_id="t1",
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            entry_price=100.0,
            quantity=1.0,
            leverage=5.0,
            position_size_usd=1000.0,
            risk_amount_usd=100.0,
            strategy_id="s1",
            agent_consensus={},
            explanation="",
            sl_price=98.0,
            tp_price=104.0,
            state=TradeLifecycleState.POSITION_CLOSED,
            entry_time=now,
            pnl=150.0,
            pnl_percent=15.0,
        )
    ]

    review = await journal.generate_daily_review(trades, date="2026-07-10")

    assert review.total_trades == 1
    assert any(e.entry_type == JournalEntryType.DAILY for e in journal.get_entries())
    assert await _wait_for(lambda: len(received) == 1)
    assert received[0].payload["total_pnl"] == 150.0


@pytest.mark.asyncio
async def test_journal_health_check_reports_state(event_bus):
    journal = JournalSystem(event_bus=event_bus)
    await journal.initialize({})
    await journal.record_mistake("t1", "test mistake")

    health = await journal.health_check()
    assert health.details["entries_recorded"] == 1

    await journal.shutdown()


@pytest.mark.asyncio
async def test_journal_publishes_learning_outcome_without_decision_id(
    event_bus, risk_engine
):
    journal = JournalSystem(event_bus=event_bus, risk_engine=risk_engine)
    await journal.initialize({})
    received = []

    async def capture(event):
        received.append(event)

    await event_bus.subscribe(
        "journal.outcome_attributed", capture, group="outcome-test"
    )
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(
        make_opportunity(agent_consensus={}), make_portfolio()
    )
    await lifecycle.update_price(trade.trade_id, 105.0)

    assert await _wait_for(lambda: len(received) == 1)
    assert received[0].payload["trade_id"] == trade.trade_id
    assert received[0].payload["decision_id"] == ""
    assert received[0].payload["pnl"] > 0
    assert received[0].payload["risk_amount_usd"] == trade.risk_amount_usd
    assert received[0].payload["strategy_id"] == trade.strategy_id


@pytest.mark.asyncio
async def test_hold_decision_is_persisted_as_management_attribution(
    event_bus, risk_engine
):
    from aitos.core.contracts import Event

    journal = JournalSystem(event_bus=event_bus, risk_engine=risk_engine)
    await journal.initialize({})
    received = []

    async def capture(event):
        received.append(event)

    await event_bus.subscribe(
        "journal.management_decision_recorded",
        capture,
        group="management-attribution-test",
    )
    await event_bus.publish(
        Event(
            topic="decision.exit",
            payload={
                "trade_id": "trade-hold-1",
                "symbol": "BTCUSDT",
                "action": "HOLD",
                "reason": "thesis_intact",
            },
            source_module="test",
        )
    )
    assert await _wait_for(lambda: len(received) == 1)
    assert received[0].payload["trade_id"] == "trade-hold-1"
    assert received[0].payload["action"] == "HOLD"
    assert received[0].payload["decision_type"] == "exit_management"


@pytest.mark.asyncio
async def test_rejection_emits_final_decision_outcome(event_bus, risk_engine):
    from aitos.core.contracts import Event

    journal = JournalSystem(event_bus=event_bus, risk_engine=risk_engine)
    await journal.initialize({})
    received = []

    async def capture(event):
        received.append(event)

    await event_bus.subscribe(
        "journal.outcome_attributed",
        capture,
        group="rejection-outcome-attribution-test",
    )
    await event_bus.publish(
        Event(
            topic="trade.rejected",
            payload={
                "trade_id": "trade-rejected-1",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "rejection_reason": "order submission failed: timeout",
            },
            source_module="test",
        )
    )
    assert await _wait_for(lambda: len(received) == 1)
    assert received[0].payload["state"] == "rejected"
    assert received[0].payload["execution_failure"] is True


@pytest.mark.asyncio
async def test_closed_trade_recovers_entry_and_management_decisions_after_restart(
    event_bus, risk_engine
):
    from aitos.core.contracts import Event

    class DurableDecisionRepository:
        def __init__(self):
            self.attributed = []

        async def get_trade_records(self, trade_id):
            assert trade_id == "trade-recovered"
            return [
                {"record_type": "TRADE_LINK", "decision_id": "entry-decision"},
                {
                    "record_type": "MANAGEMENT_DECISION",
                    "decision_id": "hold-decision",
                },
            ]

        async def attribute_outcome(self, decision_id, trade):
            self.attributed.append((decision_id, trade["trade_id"]))

    repository = DurableDecisionRepository()
    journal = JournalSystem(
        event_bus=event_bus,
        risk_engine=risk_engine,
        decision_repository=repository,
    )
    await journal.initialize({})
    journal._decision_trade_ids.clear()
    journal._decision_snapshots.clear()

    await journal._on_position_closed(
        Event(
            topic="trade.position_closed",
            payload={
                "trade_id": "trade-recovered",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "pnl": 4.0,
                "risk_amount_usd": 2.0,
            },
            source_module="test",
        )
    )

    assert repository.attributed == [
        ("entry-decision", "trade-recovered"),
        ("hold-decision", "trade-recovered"),
    ]
