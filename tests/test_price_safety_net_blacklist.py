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
async def test_blacklisted_stale_symbol_is_skipped():
    """Blacklisted symbols must NOT trigger REST fallback even when stale."""
    lifecycle = _FakeLifecycle(
        [_FakeTrade("t1", "TRUTHUSDT"), _FakeTrade("t2", "BTCUSDT")]
    )
    exchange = _FakeExchange({"TRUTHUSDT": 0.0135, "BTCUSDT": 65000.0})
    freshness = PriceFreshnessTracker()
    # neither seen live -> both stale
    net = PositionPriceSafetyNet(
        exchange,
        lambda: [lifecycle],
        freshness=freshness,
        stale_after_seconds=20.0,
        stale_symbol_blacklist=["TRUTHUSDT"],
    )

    rescued = await net.poll_once()

    assert rescued == 1  # only BTCUSDT rescued
    assert "TRUTHUSDT" not in exchange.fetch_calls
    assert "BTCUSDT" in exchange.fetch_calls
    assert lifecycle.update_price_calls == [("t2", 65000.0)]


@pytest.mark.asyncio
async def test_empty_blacklist_rescues_all():
    """Empty blacklist preserves original behavior."""
    lifecycle = _FakeLifecycle([_FakeTrade("t1", "SUSHIUSDT")])
    exchange = _FakeExchange({"SUSHIUSDT": 0.85})
    freshness = PriceFreshnessTracker()
    net = PositionPriceSafetyNet(
        exchange,
        lambda: [lifecycle],
        freshness=freshness,
        stale_after_seconds=20.0,
        stale_symbol_blacklist=[],
    )

    rescued = await net.poll_once()

    assert rescued == 1
    assert exchange.fetch_calls == ["SUSHIUSDT"]


@pytest.mark.asyncio
async def test_case_insensitive_blacklist():
    """Blacklist matching is case-insensitive."""
    lifecycle = _FakeLifecycle([_FakeTrade("t1", "truthusdt")])
    exchange = _FakeExchange({"TRUTHUSDT": 0.0135})
    freshness = PriceFreshnessTracker()
    net = PositionPriceSafetyNet(
        exchange,
        lambda: [lifecycle],
        freshness=freshness,
        stale_after_seconds=20.0,
        stale_symbol_blacklist=["truthusdt"],  # lowercase
    )

    rescued = await net.poll_once()

    assert rescued == 0
    assert exchange.fetch_calls == []
