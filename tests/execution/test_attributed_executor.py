import pytest

from aitos.execution.attributed_executor import (
    AttributedOrderExecutor,
    OrderAttributionContext,
)
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
