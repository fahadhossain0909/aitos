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
