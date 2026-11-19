"""Guarded Spot order service with filter checks and idempotent client IDs."""

from __future__ import annotations

import uuid
from decimal import Decimal

from aitos.spot.binance import BinanceSpotAdapter
from aitos.spot.filters import SpotSymbolFilters
from aitos.spot.inventory import InventoryLedger
from aitos.spot.models import SpotOrder, SpotOrderRequest, SpotOrderType


class SpotExecutionDisabled(RuntimeError):
    pass


class SpotOrderExecutor:
    def __init__(
        self,
        adapter: BinanceSpotAdapter,
        *,
        enabled: bool = False,
        allow_mainnet: bool = False,
        symbol_filters: dict[str, SpotSymbolFilters] | None = None,
    ) -> None:
        self.adapter = adapter
        self.enabled = enabled
        self.allow_mainnet = allow_mainnet
        self.filters = dict(symbol_filters or {})

    async def submit(self, request: SpotOrderRequest) -> SpotOrder:
        if not self.enabled:
            raise SpotExecutionDisabled(
                "Spot execution is disabled; explicitly enable it after approval"
            )
        if not self.adapter.testnet and not self.allow_mainnet:
            raise SpotExecutionDisabled(
                "mainnet Spot orders require allow_mainnet=True"
            )
        if not request.client_order_id:
            request = SpotOrderRequest(
                symbol=request.symbol,
                side=request.side,
                order_type=request.order_type,
                quantity=request.quantity,
                quote_order_quantity=request.quote_order_quantity,
                price=request.price,
                time_in_force=request.time_in_force,
                client_order_id="aitos-" + uuid.uuid4().hex[:28],
            )
        filters = self.filters.get(request.symbol)
        if filters is None:
            filters = (await self.adapter.exchange_info()).get(request.symbol)
        if (
            filters is None
            or filters.status != "TRADING"
            or not filters.is_spot_trading_allowed
        ):
            raise ValueError("symbol is not enabled for Spot trading")
        if request.client_order_id and len(request.client_order_id) > 36:
            raise ValueError("client_order_id must not exceed 36 characters")
        if request.quote_order_quantity is not None:
            if (
                request.order_type is not SpotOrderType.MARKET
                or not filters.quote_order_qty_market_allowed
            ):
                raise ValueError(
                    "quoteOrderQty is not supported for this symbol/order type"
                )
            if (
                filters.min_notional > 0
                and filters.apply_min_to_market
                and request.quote_order_quantity < filters.min_notional
            ):
                raise ValueError("quoteOrderQty violates minimum notional")
            if (
                filters.max_notional
                and filters.max_notional > 0
                and filters.apply_max_to_market
                and request.quote_order_quantity > filters.max_notional
            ):
                raise ValueError("quoteOrderQty exceeds maximum notional")
        elif request.quantity is not None:
            price = request.price
            if price is None:
                ticker = await self.adapter._request(
                    "GET", "/api/v3/ticker/price", params={"symbol": request.symbol}
                )
                price = Decimal(str(ticker["price"]))
            filters.validate(
                request.quantity,
                price,
                market=request.order_type is SpotOrderType.MARKET,
            )
        return await self.adapter.place_order(request)

    async def reconcile_order(
        self, symbol: str, client_order_id: str, ledger: InventoryLedger
    ) -> SpotOrder:
        """Query order and ingest authoritative trade fills exactly once.

        This is the recovery path after process restart or an ambiguous network
        response. The client order ID is queried; an uncertain POST is never
        blindly resubmitted.
        """
        order = await self.adapter.query_order(symbol, client_order_id=client_order_id)
        filters = self.filters.get(symbol.upper())
        if filters is None:
            filters = (await self.adapter.exchange_info()).get(symbol.upper())
        if filters is None:
            raise ValueError("symbol filters unavailable during fill reconciliation")
        trades = await self.adapter.my_trades(
            symbol, order_id=order.order_id, limit=1000
        )
        for trade in trades:
            trade_side = "BUY" if bool(trade.get("isBuyer")) else "SELL"
            if trade_side != order.side.value:
                raise ValueError("trade side does not match queried order")
            ledger.record_fill(
                fill_key=f"{symbol.upper()}:{int(trade['id'])}",
                symbol=symbol,
                base_asset=filters.base_asset,
                quote_asset=filters.quote_asset,
                side=trade_side,
                quantity=Decimal(str(trade["qty"])),
                price=Decimal(str(trade["price"])),
                commission=Decimal(str(trade.get("commission", "0"))),
                commission_asset=str(trade.get("commissionAsset", "")),
            )
        return order
