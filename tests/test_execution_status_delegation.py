"""Execution wrappers must preserve authoritative read-only reconciliation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aitos.trading.persistent_state import IdempotentOrderExecutor


async def test_idempotent_wrapper_forwards_authoritative_order_status():
    status = {"orderId": "order-1", "status": "PARTIALLY_FILLED", "executedQty": "0.25"}
    inner = SimpleNamespace(get_order_status=AsyncMock(return_value=status))
    wrapped = IdempotentOrderExecutor(inner)
    assert await wrapped.get_order_status("BTCUSDT", "order-1") is status
    inner.get_order_status.assert_awaited_once_with("BTCUSDT", "order-1")


async def test_status_lookup_failure_is_not_disguised_as_unsupported():
    error = TimeoutError("authoritative query unavailable")
    inner = SimpleNamespace(get_order_status=AsyncMock(side_effect=error))
    with pytest.raises(TimeoutError) as caught:
        await IdempotentOrderExecutor(inner).get_order_status("BTCUSDT", "order-1")
    assert caught.value is error


async def test_unsupported_inner_status_remains_explicitly_unknown():
    inner = SimpleNamespace(get_order_status=AsyncMock(return_value=None))
    assert (
        await IdempotentOrderExecutor(inner).get_order_status("BTCUSDT", "order-1")
        is None
    )
    inner.get_order_status.assert_awaited_once()
