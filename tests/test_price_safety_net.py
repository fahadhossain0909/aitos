from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from aitos.trading.price_freshness import PriceFreshnessTracker
from aitos.trading.price_safety_net import PositionPriceSafetyNet


@dataclass
class _Trade:
    trade_id: str
    symbol: str


class _FakeLifecycle:
    def __init__(self, trades: list[_Trade]) -> None:
        self._trades = trades
        self.updated: list[tuple[str, float]] = []

    def get_open_trades(self) -> list[_Trade]:
        return list(self._trades)

    async def update_price(
        self, trade_id: str, current_price: float, **kwargs: Any
    ) -> None:
        self.updated.append((trade_id, current_price))


class _FakeExchange:
    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = prices
        self.calls: list[str] = []

    async def fetch_recent_trades(self, symbol: str, limit: int = 500) -> list[Any]:
        self.calls.append(symbol)

        class T:
            def __init__(self, price: float) -> None:
                self.price = price

        return [T(self.prices[symbol])]


@pytest.mark.asyncio
async def test_safety_net_rescues_stale_symbol() -> None:
    freshness = PriceFreshnessTracker()
    # never noted -> stale
    lifecycle = _FakeLifecycle([_Trade("t1", "BTCUSDT")])
    exchange = _FakeExchange({"BTCUSDT": 42000.5})
    net = PositionPriceSafetyNet(
        exchange,
        lifecycles_provider=lambda: [lifecycle],
        freshness=freshness,
        stale_after_seconds=1.0,
        poll_interval_seconds=60.0,
    )
    rescued = await net.poll_once()
    assert rescued == 1
    assert lifecycle.updated == [("t1", 42000.5)]
    assert exchange.calls == ["BTCUSDT"]
    # after note_seen it should not be stale immediately
    assert not freshness.is_stale("BTCUSDT", 1.0)


@pytest.mark.asyncio
async def test_safety_net_skips_fresh_symbol() -> None:
    freshness = PriceFreshnessTracker()
    freshness.note_seen("ETHUSDT")
    lifecycle = _FakeLifecycle([_Trade("t2", "ETHUSDT")])
    exchange = _FakeExchange({"ETHUSDT": 3000.0})
    net = PositionPriceSafetyNet(
        exchange,
        lifecycles_provider=lambda: [lifecycle],
        freshness=freshness,
        stale_after_seconds=30.0,
        poll_interval_seconds=60.0,
    )
    rescued = await net.poll_once()
    assert rescued == 0
    assert lifecycle.updated == []
    assert exchange.calls == []
