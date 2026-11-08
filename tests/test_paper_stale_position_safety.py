"""Safety tests for the paper-runtime dead-candle timeout backstop."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from run_paper_trading import _close_stale_positions


class FakeLifecycle:
    def __init__(self, trade):
        self.trade = trade
        self.closed = []

    def get_open_trades(self):
        return [self.trade]

    async def close_trade(self, trade_id, price, reason):
        self.closed.append((trade_id, price, reason))


def make_components(
    last_marked_price=...,
    *,
    mae_price=0.0,
    mfe_price=0.0,
    mfe_r=0.0,
    age_hours=100.0,
):
    trade = Trade(
        trade_id="stale-paper-trade",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        quantity=1.0,
        leverage=1.0,
        position_size_usd=100.0,
        risk_amount_usd=2.0,
        strategy_id="test",
        agent_consensus={},
        explanation="test",
        sl_price=98.0,
        tp_price=104.0,
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time=(
            datetime.now(timezone.utc) - timedelta(hours=age_hours)
        ).isoformat(),
        mae_price=mae_price,
        mfe_price=mfe_price,
        mfe_r=mfe_r,
    )
    if last_marked_price is not ...:
        trade.last_marked_price = last_marked_price
        trade.last_marked_at = datetime.now(timezone.utc).isoformat()
    lifecycle = FakeLifecycle(trade)
    return SimpleNamespace(trade_lifecycle=lifecycle), lifecycle


@pytest.mark.asyncio
async def test_dead_flatline_closes_only_at_valid_mark_after_six_hour_timeout():
    components, lifecycle = make_components(100.1, mae_price=0.1, mfe_price=0.1)
    await _close_stale_positions(components)
    assert lifecycle.closed == [("stale-paper-trade", 100.1, "time_decay_scratch")]


@pytest.mark.asyncio
async def test_dead_candle_fallback_does_not_close_without_valid_mark():
    components, lifecycle = make_components()
    await _close_stale_positions(components)
    assert lifecycle.closed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_price", [None, 0.0, -1.0, float("nan"), float("inf")])
async def test_dead_candle_fallback_does_not_close_with_invalid_mark(invalid_price):
    components, lifecycle = make_components(invalid_price, mae_price=0.0, mfe_price=0.0)
    await _close_stale_positions(components)
    assert lifecycle.closed == []


@pytest.mark.asyncio
async def test_dead_candle_fallback_does_not_assume_missing_excursion_is_flat():
    components, lifecycle = make_components(100.1, mae_price=None, mfe_price=None)
    await _close_stale_positions(components)
    assert lifecycle.closed == []


@pytest.mark.asyncio
async def test_active_or_previously_moved_position_is_not_scratch_closed():
    components, lifecycle = make_components(
        100.1, mae_price=0.1, mfe_price=0.5, mfe_r=0.25
    )
    await _close_stale_positions(components)
    assert lifecycle.closed == []


@pytest.mark.asyncio
async def test_dead_candle_fallback_does_not_close_with_stale_market_mark():
    components, lifecycle = make_components(100.1, mae_price=0.0, mfe_price=0.0)
    components.trade_lifecycle.trade.last_marked_at = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).isoformat()

    await _close_stale_positions(components)

    assert lifecycle.closed == []


@pytest.mark.asyncio
async def test_position_younger_than_six_hours_is_not_closed():
    components, lifecycle = make_components(
        100.1, mae_price=0.1, mfe_price=0.1, age_hours=2.0
    )
    await _close_stale_positions(components)
    assert lifecycle.closed == []
