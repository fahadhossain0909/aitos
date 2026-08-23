import pytest

from aitos.execution.leverage_manager import configure_session_leverage
from aitos.risk.persistent_live_portfolio import PersistentLivePortfolioTracker


class FakeExecutor:
    def __init__(self):
        self.equity = 10_000.0
        self.leverage_calls = []

    async def get_account_balance(self, asset):
        return self.equity

    async def set_leverage(self, symbol, leverage):
        self.leverage_calls.append((symbol, leverage))
        return {"leverage": leverage, "maxNotionalValue": "100000"}


class FakeStore:
    def __init__(self, peak=None):
        self.peak = peak
        self.records = []

    async def load_peak_equity(self):
        return self.peak

    async def record(self, equity, peak):
        self.records.append((equity, peak))


class FakeLifecycle:
    def get_open_trades(self):
        return []


@pytest.mark.asyncio
async def test_persistent_tracker_restores_and_updates_peak():
    executor = FakeExecutor()
    store = FakeStore(12_000.0)
    tracker = PersistentLivePortfolioTracker(executor, store)

    await tracker.initialize()
    executor.equity = 11_000.0
    await tracker.refresh_equity()

    assert tracker.peak_equity_usd == 12_000.0
    assert store.records[-1] == (11_000.0, 12_000.0)
    assert (
        tracker.build_portfolio_state(FakeLifecycle()).current_drawdown_pct
        == pytest.approx(8.3333333333)
    )


@pytest.mark.asyncio
async def test_persistent_tracker_raises_peak_after_restart():
    executor = FakeExecutor()
    store = FakeStore()
    tracker = PersistentLivePortfolioTracker(executor, store)

    await tracker.initialize()
    await tracker.refresh_equity()

    assert tracker.peak_equity_usd == 10_000.0
    assert store.records[-1] == (10_000.0, 10_000.0)


@pytest.mark.asyncio
async def test_session_leverage_is_applied_to_all_symbols():
    executor = FakeExecutor()

    await configure_session_leverage(executor, ["BTCUSDT", "ETHUSDT"], 3)

    assert executor.leverage_calls == [("BTCUSDT", 3), ("ETHUSDT", 3)]


@pytest.mark.asyncio
async def test_session_leverage_rejects_fractional_tier():
    executor = FakeExecutor()

    with pytest.raises(ValueError, match="positive integer"):
        await configure_session_leverage(executor, ["BTCUSDT"], 3.5)
