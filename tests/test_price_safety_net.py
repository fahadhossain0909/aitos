import asyncio

import pytest

from aitos.trading.price_freshness import PriceFreshnessTracker
from aitos.trading.price_safety_net import PositionPriceSafetyNet


class _FakeTrade:
    def __init__(self, trade_id: str, symbol: str) -> None:
        self.trade_id = trade_id
        self.symbol = symbol


class _FakeTick:
    def __init__(self, price: float) -> None:
        self.price = price


class _FakeLifecycle:
    def __init__(self, trades: list[_FakeTrade]) -> None:
        self._trades = trades
        self.update_price_calls: list[tuple[str, float]] = []

    def get_open_trades(self) -> list[_FakeTrade]:
        return list(self._trades)

    async def update_price(self, trade_id: str, current_price: float, **_kwargs):
        self.update_price_calls.append((trade_id, current_price))


class _FakeExchange:
    def __init__(self, prices: dict[str, float]) -> None:
        self._prices = prices
        self.fetch_calls: list[str] = []

    async def fetch_recent_trades(self, symbol: str, limit: int = 500):
        self.fetch_calls.append(symbol)
        if symbol not in self._prices:
            return []
        return [_FakeTick(self._prices[symbol])]


@pytest.mark.asyncio
async def test_stale_symbol_gets_rest_price_and_reaches_update_price():
    lifecycle = _FakeLifecycle([_FakeTrade("t1", "SUSHIUSDT")])
    exchange = _FakeExchange({"SUSHIUSDT": 0.85})
    freshness = PriceFreshnessTracker()
    # never seen live -> immediately stale
    net = PositionPriceSafetyNet(
        exchange,
        lambda: [lifecycle],
        freshness=freshness,
        stale_after_seconds=20.0,
    )

    rescued = await net.poll_once()

    assert rescued == 1
    assert exchange.fetch_calls == ["SUSHIUSDT"]
    assert lifecycle.update_price_calls == [("t1", 0.85)]


@pytest.mark.asyncio
async def test_fresh_symbol_is_not_polled():
    lifecycle = _FakeLifecycle([_FakeTrade("t1", "BTCUSDT")])
    exchange = _FakeExchange({"BTCUSDT": 65000.0})
    freshness = PriceFreshnessTracker()
    freshness.note_seen("BTCUSDT")  # just seen live
    net = PositionPriceSafetyNet(
        exchange,
        lambda: [lifecycle],
        freshness=freshness,
        stale_after_seconds=20.0,
    )

    rescued = await net.poll_once()

    assert rescued == 0
    assert exchange.fetch_calls == []
    assert lifecycle.update_price_calls == []


@pytest.mark.asyncio
async def test_live_update_marks_symbol_fresh_again():
    freshness = PriceFreshnessTracker()
    assert freshness.is_stale("ETHUSDT", 20.0) is True
    freshness.note_seen("ETHUSDT")
    assert freshness.is_stale("ETHUSDT", 20.0) is False


@pytest.mark.asyncio
async def test_start_stop_runs_and_shuts_down_cleanly():
    lifecycle = _FakeLifecycle([_FakeTrade("t1", "SOLUSDT")])
    exchange = _FakeExchange({"SOLUSDT": 150.0})
    net = PositionPriceSafetyNet(
        exchange,
        lambda: [lifecycle],
        stale_after_seconds=0.0,
        poll_interval_seconds=0.01,
    )
    net.start()
    for _ in range(200):
        if lifecycle.update_price_calls:
            break
        await asyncio.sleep(0.01)
    await net.stop()

    assert lifecycle.update_price_calls
    assert net.polls_run >= 1
