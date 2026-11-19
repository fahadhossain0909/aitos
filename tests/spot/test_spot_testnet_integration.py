"""Opt-in Binance Spot Testnet lifecycle verification.

This test is skipped unless dedicated Spot Testnet credentials are supplied.
It never targets mainnet.
"""

import os
import uuid
from decimal import Decimal, ROUND_UP

import pytest

from aitos.spot.binance import BinanceSpotAdapter
from aitos.spot.execution import SpotOrderExecutor
from aitos.spot.filters import SpotSymbolFilters
from aitos.spot.inventory import InventoryLedger
from aitos.spot.models import (
    SpotOrderRequest,
    SpotOrderSide,
    SpotOrderStatus,
    SpotOrderType,
)


@pytest.mark.asyncio
async def test_binance_spot_testnet_limit_order_cancel_and_reconcile(tmp_path):
    api_key = os.getenv("AITOS_SPOT_API_KEY", "").strip()
    api_secret = os.getenv("AITOS_SPOT_API_SECRET", "").strip()
    if not api_key or not api_secret:
        pytest.skip("dedicated Binance Spot Testnet credentials are not configured")

    adapter = BinanceSpotAdapter(
        api_key=api_key,
        api_secret=api_secret,
        testnet=True,
    )
    ledger_path = tmp_path / "spot-testnet-recovery.sqlite3"
    ledger = InventoryLedger(ledger_path)
    try:
        await adapter.sync_server_time()
        filters = await adapter.exchange_info()
        symbol_filters = filters.get("BTCUSDT")
        if (
            symbol_filters is None
            or symbol_filters.status != "TRADING"
            or not symbol_filters.is_spot_trading_allowed
        ):
            pytest.skip("BTCUSDT is not available on this Spot Testnet environment")

        ticker = await adapter.ticker_24h("BTCUSDT")
        last_price = Decimal(str(ticker["lastPrice"]))
        price = symbol_filters.round_price(last_price * Decimal("0.5"))
        if price <= 0:
            pytest.skip("testnet BTCUSDT price is unavailable")

        step = symbol_filters.lot_step_size
        minimum = max(
            symbol_filters.lot_min_qty,
            (
                (symbol_filters.min_notional / price)
                if symbol_filters.min_notional > 0
                else Decimal("0")
            ),
        )
        quantity = (
            (minimum / step).to_integral_value(rounding=ROUND_UP) * step
            if step > 0
            else minimum
        )
        if quantity <= 0 or (
            symbol_filters.lot_max_qty > 0 and quantity > symbol_filters.lot_max_qty
        ):
            pytest.skip("testnet symbol filters do not allow a safe minimum test order")
        symbol_filters.validate(quantity, price)

        request = SpotOrderRequest(
            symbol="BTCUSDT",
            side=SpotOrderSide.BUY,
            order_type=SpotOrderType.LIMIT,
            quantity=quantity,
            price=price,
            time_in_force="GTC",
            client_order_id="aitos-test-" + uuid.uuid4().hex[:12],
        )
        executor = SpotOrderExecutor(
            adapter,
            enabled=True,
            symbol_filters={"BTCUSDT": symbol_filters},
        )
        ledger.record_order_intent(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side.value,
            order_type=request.order_type.value,
            quantity=request.quantity,
        )
        placed = await executor.submit(request)
        ledger.update_order_intent(
            request.client_order_id,
            status=placed.status.value,
            exchange_order_id=placed.order_id,
        )
        queried = await adapter.query_order(
            "BTCUSDT", client_order_id=request.client_order_id
        )
        assert queried.order_id == placed.order_id

        if queried.status in {SpotOrderStatus.NEW, SpotOrderStatus.PARTIALLY_FILLED}:
            await adapter.cancel_order(
                "BTCUSDT", client_order_id=request.client_order_id
            )

        ledger.close()
        ledger = InventoryLedger(ledger_path)
        reconciled = await executor.reconcile_order(
            "BTCUSDT", request.client_order_id, ledger
        )
        ledger.update_order_intent(
            request.client_order_id,
            status=reconciled.status.value,
            exchange_order_id=reconciled.order_id,
        )
        assert ledger.unresolved_order_intents() == []
        assert reconciled.status in {
            SpotOrderStatus.FILLED,
            SpotOrderStatus.CANCELED,
            SpotOrderStatus.EXPIRED,
        }
        assert reconciled.executed_quantity >= 0
    finally:
        ledger.close()
        await adapter.close()
