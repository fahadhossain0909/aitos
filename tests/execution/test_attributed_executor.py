import re
from unittest.mock import AsyncMock

import pytest
from aioresponses import aioresponses

from aitos.execution.attributed_executor import (
    AttributionPersistenceError,
    AttributedOrderExecutor,
    OrderAttributionContext,
)
from aitos.execution.binance_executor import TESTNET_URL, BinanceFuturesOrderExecutor
from aitos.execution.ctrader_executor import CTraderOrderExecutor
from aitos.execution.mt5_gateway import MT5GatewayOrderExecutor
from aitos.execution.order_executor import (
    OrderExecutor,
    OrderRequest,
    OrderResult,
    SimulatedOrderExecutor,
)
from aitos.models.trade import TradeSide
from aitos.statistics.decision_ledger import DecisionOutcomeLedger


def _context(request):
    return OrderAttributionContext(
        decision_id=f"{request.client_order_id}:decision",
        entity_id="scan-trace-42",
        reason="scanner-approved-entry",
        payload={"trace_id": "scan-trace-42"},
    )


@pytest.mark.asyncio
async def test_attributed_executor_records_successful_entry_and_final_outcome(tmp_path):
    ledger = DecisionOutcomeLedger(tmp_path / "decisions.sqlite3")
    executor = AttributedOrderExecutor(SimulatedOrderExecutor(), ledger, _context)
    result = await executor.submit_order(
        OrderRequest(
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            quantity=0.25,
            reference_price=100.0,
            client_order_id="client-entry-42",
        )
    )
    rows = ledger.list_decisions(entity_id="scan-trace-42")
    assert result.success
    assert len(rows) == 1
    assert rows[0]["decision_type"] == "ENTRY"
    assert rows[0]["payload"]["venue_order_id"] == result.order_id
    assert (
        ledger.resolve_entity(
            "scan-trace-42",
            outcome_value=0.012,
            outcome_at="2026-11-07T12:00:00+00:00",
            outcome={"net_pnl_usd": 1.2},
        )
        == 1
    )


class _RejectingExecutor(OrderExecutor):
    async def submit_order(self, request):
        return OrderResult(
            order_id="venue-rejected",
            symbol=request.symbol,
            side=request.side,
            filled_quantity=0.0,
            fill_price=0.0,
            success=False,
            error="insufficient margin",
        )


@pytest.mark.asyncio
async def test_attributed_executor_records_rejection_and_execution_failure(tmp_path):
    ledger = DecisionOutcomeLedger(tmp_path / "decisions.sqlite3")
    executor = AttributedOrderExecutor(_RejectingExecutor(), ledger, _context)
    result = await executor.submit_order(
        OrderRequest(
            symbol="ETHUSDT",
            side=TradeSide.SHORT,
            quantity=1.0,
            reference_price=2000.0,
            client_order_id="client-entry-fail",
        )
    )
    rows = ledger.list_decisions(entity_id="scan-trace-42")
    assert not result.success
    assert {row["decision_type"] for row in rows} == {"ENTRY", "EXECUTION_FAILURE"}
    assert all(not row["accepted"] for row in rows)


@pytest.mark.asyncio
async def test_attribution_decorator_wraps_binance_testnet_adapter(tmp_path):
    ledger = DecisionOutcomeLedger(tmp_path / "binance-decisions.sqlite3")
    adapter = BinanceFuturesOrderExecutor(api_key="test-key", api_secret="test-secret")
    await adapter.connect()
    executor = AttributedOrderExecutor(adapter, ledger, _context)
    with aioresponses() as mocked:
        mocked.post(
            re.compile(r"^" + re.escape(TESTNET_URL + "/fapi/v1/order") + r".*"),
            payload={
                "orderId": 731,
                "status": "FILLED",
                "executedQty": "0.5",
                "avgPrice": "100.25",
            },
        )
        result = await executor.submit_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=TradeSide.LONG,
                quantity=0.5,
                reference_price=100.0,
                client_order_id="binance-client-731",
            )
        )
    assert result.success
    rows = ledger.list_decisions(entity_id="scan-trace-42")
    assert rows[0]["payload"]["venue_order_id"] == "731"
    await adapter.close()


@pytest.mark.asyncio
async def test_attribution_decorator_wraps_mt5_gateway_adapter(tmp_path):
    ledger = DecisionOutcomeLedger(tmp_path / "mt5-decisions.sqlite3")
    adapter = MT5GatewayOrderExecutor("http://mt5.test", "test-token")
    executor = AttributedOrderExecutor(adapter, ledger, _context)
    with aioresponses() as mocked:
        mocked.post(
            "http://mt5.test/v1/orders",
            payload={
                "order_id": "mt5-44",
                "filled_quantity": 0.2,
                "fill_price": 2000.5,
                "success": True,
            },
        )
        result = await executor.submit_order(
            OrderRequest(
                symbol="XAUUSD",
                side=TradeSide.SHORT,
                quantity=0.2,
                reference_price=2001.0,
                client_order_id="mt5-client-44",
            )
        )
    assert result.success
    rows = ledger.list_decisions(entity_id="scan-trace-42")
    assert rows[0]["payload"]["venue_order_id"] == "mt5-44"


@pytest.mark.asyncio
async def test_attribution_decorator_wraps_ctrader_executor_contract(tmp_path):
    pytest.importorskip("ctrader_open_api")
    ledger = DecisionOutcomeLedger(tmp_path / "ctrader-decisions.sqlite3")
    adapter = object.__new__(CTraderOrderExecutor)
    adapter.submit_order = AsyncMock(
        return_value=OrderResult(
            order_id="ctrader-91",
            symbol="EURUSD",
            side=TradeSide.LONG,
            filled_quantity=0.1,
            fill_price=1.085,
            success=True,
        )
    )
    executor = AttributedOrderExecutor(adapter, ledger, _context)
    result = await executor.submit_order(
        OrderRequest(
            symbol="EURUSD",
            side=TradeSide.LONG,
            quantity=0.1,
            reference_price=1.084,
            client_order_id="ctrader-client-91",
        )
    )
    assert result.success
    rows = ledger.list_decisions(entity_id="scan-trace-42")
    assert rows[0]["payload"]["venue_order_id"] == "ctrader-91"


class _RaisingExecutor(OrderExecutor):
    async def submit_order(self, request):
        raise TimeoutError("venue response timed out")


@pytest.mark.asyncio
async def test_attributed_executor_records_ambiguous_transport_failure_before_reraising(
    tmp_path,
):
    ledger = DecisionOutcomeLedger(tmp_path / "timeout-decisions.sqlite3")
    executor = AttributedOrderExecutor(_RaisingExecutor(), ledger, _context)
    with pytest.raises(TimeoutError):
        await executor.submit_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=TradeSide.LONG,
                quantity=0.1,
                reference_price=100.0,
                client_order_id="timeout-client",
            )
        )
    rows = ledger.list_decisions(entity_id="scan-trace-42")
    assert {row["decision_type"] for row in rows} == {"ENTRY", "EXECUTION_FAILURE"}
    assert any("TimeoutError" in row["payload"]["execution_error"] for row in rows)


class _BrokenLedger:
    def record_decision(self, *args, **kwargs):
        raise OSError("journal unavailable")


@pytest.mark.asyncio
async def test_attributed_executor_exposes_venue_result_when_ledger_persistence_fails():
    executor = AttributedOrderExecutor(
        SimulatedOrderExecutor(), _BrokenLedger(), _context
    )
    with pytest.raises(AttributionPersistenceError) as error:
        await executor.submit_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=TradeSide.LONG,
                quantity=0.1,
                reference_price=100.0,
                client_order_id="ledger-failure-client",
            )
        )
    assert error.value.may_have_executed
    assert error.value.order_result.success
    assert error.value.order_result.order_id.startswith("sim-")


class _ManagedSimulatedExecutor(SimulatedOrderExecutor):
    @property
    def supports_exchange_side_stops(self):
        return True

    async def cancel_resting_order(self, symbol, order_id):
        self.cancelled_order = (symbol, order_id)


@pytest.mark.asyncio
async def test_attributed_executor_records_resting_order_cancellation(tmp_path):
    ledger = DecisionOutcomeLedger(tmp_path / "cancel-decisions.sqlite3")
    adapter = _ManagedSimulatedExecutor()
    executor = AttributedOrderExecutor(adapter, ledger, _context)
    result = await executor.submit_order(
        OrderRequest(
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            quantity=0.1,
            reference_price=100.0,
            client_order_id="managed-client",
        )
    )
    await executor.cancel_resting_order("BTCUSDT", result.order_id)
    rows = ledger.list_decisions(entity_id="scan-trace-42")
    assert adapter.cancelled_order == ("BTCUSDT", result.order_id)
    assert {row["decision_type"] for row in rows} == {"ENTRY", "MANAGE"}
    assert any(row["payload"].get("action") == "CANCEL" for row in rows)


@pytest.mark.asyncio
async def test_mt5_gateway_fails_closed_for_unmonitored_resting_orders():
    adapter = MT5GatewayOrderExecutor("http://mt5.test", "test-token")
    with pytest.raises(ValueError, match="status polling"):
        await adapter.submit_order(
            OrderRequest(
                symbol="EURUSD",
                side=TradeSide.LONG,
                quantity=1.0,
                reference_price=1.08,
                order_type="LIMIT",
            )
        )


@pytest.mark.asyncio
async def test_ctrader_fails_closed_for_unmonitored_resting_orders():
    adapter = object.__new__(CTraderOrderExecutor)
    with pytest.raises(ValueError, match="status polling"):
        await adapter.submit_order(
            OrderRequest(
                symbol="EURUSD",
                side=TradeSide.LONG,
                quantity=1.0,
                reference_price=1.08,
                order_type="STOP",
            )
        )


class _PartialStatusErrorExecutor(OrderExecutor):
    async def submit_order(self, request):
        return OrderResult(
            order_id="venue-partial-1",
            symbol=request.symbol,
            side=request.side,
            filled_quantity=0.2,
            fill_price=request.reference_price,
            success=False,
            error="venue reported cancellation after partial fill",
        )


@pytest.mark.asyncio
async def test_attribution_marks_confirmed_partial_fill_accepted_and_failure_separately(
    tmp_path,
):
    ledger = DecisionOutcomeLedger(tmp_path / "partial-status-decisions.sqlite3")
    executor = AttributedOrderExecutor(_PartialStatusErrorExecutor(), ledger, _context)
    result = await executor.submit_order(
        OrderRequest(
            symbol="BTCUSDT",
            side=TradeSide.LONG,
            quantity=0.5,
            reference_price=100.0,
            client_order_id="partial-status-client",
        )
    )
    rows = ledger.list_decisions(entity_id="scan-trace-42")
    entry = next(row for row in rows if row["decision_type"] == "ENTRY")
    failure = next(row for row in rows if row["decision_type"] == "EXECUTION_FAILURE")
    assert not result.success
    assert entry["accepted"] is True
    assert failure["accepted"] is False
