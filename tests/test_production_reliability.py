import pytest

from aitos.exchange.symbol_filter_refresher import SymbolFilterRefresher
from aitos.exchange.symbol_filters import SymbolFilters
from aitos.execution.order_executor import OrderRequest, SimulatedOrderExecutor
from aitos.models.trade import (
    Opportunity,
    Trade,
    TradeLifecycleState,
    TradeSide,
)
from aitos.trading.persistent_state import (
    AmbiguousOrderSubmissionError,
    IdempotentOrderExecutor,
    TradeStatePersistence,
    _trade_from_dict,
)


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


class FakeDurableOrderStore:
    def __init__(self):
        self.records = {}
        self.fail_ack_once = False

    async def load_order_record(self, client_order_id):
        return self.records.get(client_order_id)

    async def load_pending_order_intents(self):
        return [
            {"client_order_id": key, **record}
            for key, record in self.records.items()
            if record["state"] == "SUBMITTING"
        ]

    async def load_unlinked_acknowledged_orders(self):
        return []

    async def save_order_intent(self, client_order_id, request):
        self.records[client_order_id] = {
            "symbol": request["symbol"],
            "state": "SUBMITTING",
            "request": request,
            "result": {},
        }

    async def save_order_result(self, client_order_id, symbol, result):
        if self.fail_ack_once:
            self.fail_ack_once = False
            raise OSError("simulated lost acknowledgement persistence")
        self.records[client_order_id] = {
            "symbol": symbol,
            "state": "ACKNOWLEDGED" if result["success"] else "REJECTED",
            "request": self.records[client_order_id]["request"],
            "result": result,
        }


@pytest.mark.asyncio
async def test_durable_order_executor_recovers_lost_ack_without_resubmitting():
    inner = CountingExecutor()
    store = FakeDurableOrderStore()
    store.fail_ack_once = True
    request = OrderRequest(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=0.01,
        reference_price=100_000.0,
        client_order_id="aitos-entry-crash-window-1",
    )

    first_process = IdempotentOrderExecutor(inner, store)
    with pytest.raises(OSError, match="lost acknowledgement"):
        await first_process.submit_order(request)

    second_process = IdempotentOrderExecutor(inner, store)
    recovered = await second_process.submit_order(request)

    assert recovered.order_id == "sim-1"
    assert recovered.filled_quantity == pytest.approx(0.01)
    assert inner.calls == 1
    assert store.records[request.client_order_id]["state"] == "ACKNOWLEDGED"


@pytest.mark.asyncio
async def test_durable_order_executor_blocks_resubmit_when_venue_lookup_is_ambiguous(
):
    class NoLookupExecutor(CountingExecutor):
        async def get_order_by_client_id(self, symbol, client_order_id):
            return None

    inner = NoLookupExecutor()
    store = FakeDurableOrderStore()
    request = OrderRequest(
        symbol="ETHUSDT",
        side=TradeSide.SHORT,
        quantity=0.02,
        reference_price=3_000.0,
        client_order_id="aitos-entry-ambiguous-1",
    )
    await store.save_order_intent(
        request.client_order_id,
        IdempotentOrderExecutor._request_payload(request),
    )

    executor = IdempotentOrderExecutor(inner, store)
    with pytest.raises(
        AmbiguousOrderSubmissionError,
        match="automatic resubmission blocked",
    ):
        await executor.submit_order(request)

    assert inner.calls == 0
    assert store.records[request.client_order_id]["state"] == "SUBMITTING"


@pytest.mark.asyncio
async def test_startup_reconciliation_blocks_unlinked_recovered_fill():
    inner = CountingExecutor()
    store = FakeDurableOrderStore()
    request = OrderRequest(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=0.01,
        reference_price=100_000.0,
        client_order_id="aitos-entry-unlinked-1",
    )
    await store.save_order_intent(
        request.client_order_id,
        IdempotentOrderExecutor._request_payload(request),
    )
    await inner.submit_order(request)

    executor = IdempotentOrderExecutor(inner, store)
    recovery = await executor.reconcile_pending_orders()

    assert recovery["pending"] == 1
    assert recovery["filled_unlinked"] == [request.client_order_id]
    assert recovery["terminal_unfilled"] == []


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
        exchange,
        sink,
        ["BTCUSDT"],
        ttl_seconds=60.0,
    )

    await refresher.start()
    try:
        assert exchange.calls == 1
        assert sink.filters["BTCUSDT"].tick_size == 0.1
        assert refresher.refresh_count == 1
    finally:
        await refresher.stop()


class FakeStateStore:
    def __init__(self, trades):
        self.trades = trades

    async def load_open_trades(self):
        return list(self.trades)

    async def initialize(self):
        return None


class FakeLifecycle:
    def __init__(self):
        self._open_trades = {}


@pytest.mark.asyncio
async def test_open_trade_is_restored_after_restart(event_bus):
    trade = Trade(
        trade_id="trade-restart",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        quantity=1.0,
        leverage=2.0,
        position_size_usd=100.0,
        risk_amount_usd=5.0,
        strategy_id="test",
        agent_consensus={},
        explanation="restart recovery",
        sl_price=95.0,
        tp_price=110.0,
        take_profit_levels=[110.0],
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time="2026-08-24T03:00:00+00:00",
    )
    lifecycle = FakeLifecycle()
    persistence = TradeStatePersistence(
        event_bus,
        lifecycle,
        FakeStateStore([trade]),
    )

    restored = await persistence.restore()

    assert restored == 1
    assert lifecycle._open_trades["trade-restart"].symbol == "BTCUSDT"
    assert (
        lifecycle._open_trades["trade-restart"].state
        == TradeLifecycleState.POSITION_OPENED
    )


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
    assert trade.sl_price >= protected_sl


def test_adaptive_exit_observation_counter_survives_trade_recovery():
    trade = Trade(
        trade_id="trade-adaptive-restart",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        quantity=1.0,
        leverage=2.0,
        position_size_usd=100.0,
        risk_amount_usd=5.0,
        strategy_id="test",
        agent_consensus={},
        explanation="adaptive restart",
        sl_price=95.0,
        tp_price=110.0,
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time="2026-08-24T03:00:00+00:00",
        adaptive_exit_observations=17,
    )

    restored = _trade_from_dict(trade.to_dict())

    assert restored.adaptive_exit_observations == 17
