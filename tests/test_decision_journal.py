import asyncio
from dataclasses import replace

import pytest

from aitos.core.contracts import Event
from aitos.journal.journal_system import JournalSystem
from aitos.models.trade import Opportunity, TradeLifecycleState, TradeSide
from aitos.risk.models import PortfolioState
from aitos.trading.lifecycle import TradeLifecycle


class FakeDecisionRepository:
    def __init__(self):
        self.decisions = []
        self.links = []
        self.outcomes = []

    async def save_decision(self, decision_id, snapshot):
        self.decisions.append((decision_id, snapshot))

    async def link_trade(self, decision_id, trade):
        self.links.append((decision_id, trade))

    async def attribute_outcome(self, decision_id, trade):
        self.outcomes.append((decision_id, trade))


def make_opportunity(**overrides):
    values = dict(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        stop_loss_price=98.0,
        take_profit_levels=[104.0],
        confidence=0.8,
        strategy_id="test-strategy",
        rationale="test rationale",
        agent_consensus={"order_flow_bias": 7.0, "liquidity_quality": 8.0},
    )
    values.update(overrides)
    return Opportunity(**values)


async def _wait_for(predicate, timeout=3.0):
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(0.02)
        elapsed += 0.02
    return False


@pytest.mark.asyncio
async def test_decision_snapshot_is_persisted_and_linked_to_trade(
    event_bus, risk_engine
):
    repo = FakeDecisionRepository()
    journal = JournalSystem(
        event_bus=event_bus, risk_engine=risk_engine, decision_repository=repo
    )
    await journal.initialize({})

    opportunity = make_opportunity()
    await event_bus.publish(
        Event(
            topic="decision.snapshot",
            payload={
                "decision_id": opportunity.opportunity_id,
                "symbol": opportunity.symbol,
                "side": opportunity.side.value,
                "entry_price": opportunity.entry_price,
                "confidence": opportunity.confidence,
                "strategy_id": opportunity.strategy_id,
                "regime": opportunity.regime,
                "agent_consensus": opportunity.agent_consensus,
            },
            source_module="test",
        )
    )

    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    opportunity = replace(
        opportunity, agent_consensus={"decision_id": opportunity.opportunity_id}
    )
    trade = await lifecycle.submit_opportunity(
        opportunity, PortfolioState(equity_usd=10_000.0, peak_equity_usd=10_000.0)
    )

    assert await _wait_for(lambda: len(repo.decisions) == 1)
    assert await _wait_for(lambda: len(repo.links) == 1)
    assert repo.decisions[0][0] == opportunity.opportunity_id
    assert repo.links[0][0] == opportunity.opportunity_id
    assert (
        journal.get_decision_snapshot(opportunity.opportunity_id)["symbol"] == "BTCUSDT"
    )
    assert journal.get_decision_trade_id(opportunity.opportunity_id) == trade.trade_id


@pytest.mark.asyncio
async def test_closed_trade_is_attributed_to_original_decision(event_bus, risk_engine):
    repo = FakeDecisionRepository()
    journal = JournalSystem(
        event_bus=event_bus, risk_engine=risk_engine, decision_repository=repo
    )
    await journal.initialize({})

    opportunity = make_opportunity()
    await event_bus.publish(
        Event(
            topic="decision.snapshot",
            payload={
                "decision_id": opportunity.opportunity_id,
                "symbol": opportunity.symbol,
                "side": opportunity.side.value,
                "entry_price": opportunity.entry_price,
                "confidence": opportunity.confidence,
                "strategy_id": opportunity.strategy_id,
                "regime": opportunity.regime,
            },
            source_module="test",
        )
    )

    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(
        make_opportunity(agent_consensus={"decision_id": opportunity.opportunity_id}),
        PortfolioState(equity_usd=10_000.0, peak_equity_usd=10_000.0),
    )
    await lifecycle.update_price(trade.trade_id, 105.0)

    assert await _wait_for(lambda: len(repo.outcomes) == 1)
    decision_id, outcome = repo.outcomes[0]
    assert decision_id == opportunity.opportunity_id
    assert outcome["trade_id"] == trade.trade_id
    assert outcome["state"] == TradeLifecycleState.POSITION_CLOSED.value
    assert outcome["pnl"] > 0


@pytest.mark.asyncio
async def test_backward_compatible_decision_opportunity_is_captured(event_bus):
    repo = FakeDecisionRepository()
    journal = JournalSystem(event_bus=event_bus, decision_repository=repo)
    await journal.initialize({})

    await event_bus.publish(
        Event(
            topic="decision.opportunity",
            payload={"symbol": "BTCUSDT", "side": "LONG", "confidence": 0.7},
            source_module="test",
        )
    )

    assert await _wait_for(lambda: len(repo.decisions) == 1)
    assert repo.decisions[0][1]["symbol"] == "BTCUSDT"
