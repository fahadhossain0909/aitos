import pytest

from aitos.execution.order_executor import OrderRequest, SimulatedOrderExecutor
from aitos.models.trade import TradeSide


@pytest.mark.asyncio
async def test_simulated_executor_fills_full_quantity_at_reference_price():
    executor = SimulatedOrderExecutor()
    result = await executor.submit_order(
        OrderRequest(
            symbol="BTCUSDT", side=TradeSide.LONG, quantity=1.5, reference_price=100.0
        )
    )
    assert result.success is True
    assert result.filled_quantity == 1.5
    assert result.fill_price == 100.0


@pytest.mark.asyncio
async def test_simulated_executor_applies_slippage_against_long_and_short():
    executor = SimulatedOrderExecutor(slippage_bps=10.0)  # 0.1%
    long_result = await executor.submit_order(
        OrderRequest(
            symbol="BTCUSDT", side=TradeSide.LONG, quantity=1.0, reference_price=100.0
        )
    )
    short_result = await executor.submit_order(
        OrderRequest(
            symbol="BTCUSDT", side=TradeSide.SHORT, quantity=1.0, reference_price=100.0
        )
    )
    assert long_result.fill_price > 100.0  # buying pays a bit more
    assert short_result.fill_price < 100.0  # selling receives a bit less


@pytest.mark.asyncio
async def test_simulated_executor_generates_unique_order_ids():
    executor = SimulatedOrderExecutor()
    r1 = await executor.submit_order(
        OrderRequest(
            symbol="BTCUSDT", side=TradeSide.LONG, quantity=1.0, reference_price=100.0
        )
    )
    r2 = await executor.submit_order(
        OrderRequest(
            symbol="BTCUSDT", side=TradeSide.LONG, quantity=1.0, reference_price=100.0
        )
    )
    assert r1.order_id != r2.order_id
