"""Safety tests for the paper-trading stale-position timeout."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aitos.models.trade import TradeLifecycleState
from run_paper_trading import _close_stale_positions


class FakeLifecycle:
    def __init__(self, trade):
        self.trade = trade
        self.closed = []

    def get_open_trades(self):
        return [self.trade]

    async def close_trade(self, trade_id, price, reason):
        self.closed.append((trade_id, price, reason))


def make_components(last_marked_price=...):
    trade = SimpleNamespace(
        trade_id="stale-paper-trade",
        symbol="BTCUSDT",
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time="2000-01-01T00:00:00+00:00",
        entry_price=100.0,
    )
    if last_marked_price is not ...:
        trade.last_marked_price = last_marked_price
    lifecycle = FakeLifecycle(trade)
    return SimpleNamespace(trade_lifecycle=lifecycle), lifecycle


@pytest.mark.asyncio
async def test_stale_position_is_not_closed_at_entry_price_when_mark_is_missing():
    components, lifecycle = make_components()

    await _close_stale_positions(components)

    assert lifecycle.closed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_price", [None, 0.0, -1.0, float("nan"), float("inf")])
async def test_stale_position_is_not_closed_with_invalid_mark(invalid_price):
    components, lifecycle = make_components(invalid_price)

    await _close_stale_positions(components)

    assert lifecycle.closed == []


@pytest.mark.asyncio
async def test_stale_position_closes_at_a_valid_last_mark():
    components, lifecycle = make_components(101.25)

    await _close_stale_positions(components)

    assert lifecycle.closed == [
        ("stale-paper-trade", 101.25, "stale_position_timeout")
    ]
