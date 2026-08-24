import pytest

from aitos.exchange.symbol_filter_refresher import SymbolFilterRefresher
from aitos.exchange.symbol_filters import SymbolFilters
from aitos.execution.order_executor import OrderRequest, SimulatedOrderExecutor
from aitos.models.trade import Opportunity, TradeSide
from aitos.trading.persistent_state import IdempotentOrderExecutor


class CountingExecutor(SimulatedOrderExecutor):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def submit_order(self, request):
        self.calls += 1
        return await super().submit_order(request)


@pytest.mark.asyncio
async def test_order_executor_is_idempotent_for_repeated_request():
    inner = CountingExecutor()
    executor = IdempotentOrderExecutor(inner)
    request = OrderRequest(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=0.01,
        reference_price=100_000.0,
    )

    first = await executor.submit_order(request)
    second = await executor.submit_order(request)

    assert first.order_id == second.order_id
    assert inner.calls == 1


class FakeExchange:
    def __init__(self):
        self.calls = 0

    async def fetch_exchange_info(self, symbols):
        self.calls += 1
        return {
            symbol: SymbolFilters(
                symbol=symbol,
                step_size=0.001,
                tick_size=0.1,
                min_notional=5.0,
                quantity_precision=3,
                price_precision=1,
            )
            for symbol in symbols
        }


class FilterSink:
    def __init__(self):
        self.filters = {}

    def load_symbol_filters(self, filters):
        self.filters.update(filters)


@pytest.mark.asyncio
async def test_symbol_filter_refresher_loads_filters_and_stops():
    exchange = FakeExchange()
    sink = FilterSink()
    refresher = SymbolFilterRefresher(
        exchange, sink, ["BTCUSDT"], ttl_seconds=60.0
    )

    await refresher.start()
    try:
        assert exchange.calls == 1
        assert sink.filters["BTCUSDT"].tick_size == 0.1
        assert refresher.refresh_count == 1
    finally:
        await refresher.stop()


@pytest.mark.asyncio
async def test_dynamic_exit_protects_a_trailing_stop(event_bus, risk_engine):
    from aitos.risk.models import PortfolioState
    from aitos.trading.lifecycle import TradeLifecycle

    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    opportunity = Opportunity(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        stop_loss_price=95.0,
        take_profit_levels=[110.0, 120.0],
        confidence=0.9,
        strategy_id="test",
        rationale="dynamic exit test",
        trailing_sl_enabled=True,
    )
    portfolio = PortfolioState(
        equity_usd=10_000.0,
        peak_equity_usd=10_000.0,
        volatility_percentile=30.0,
        max_pairwise_correlation=0.1,
    )
    trade = await lifecycle.submit_opportunity(opportunity, portfolio)
    original_sl = trade.sl_price
    await lifecycle.update_price(trade.trade_id, 104.0)
    protected_sl = trade.sl_price
    await lifecycle.update_price(trade.trade_id, 102.0)

    assert protected_sl > original_sl
    assert trade.sl_price == protected_sl
