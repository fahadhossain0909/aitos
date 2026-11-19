"""Guarded Spot order service with filter checks and idempotent client IDs."""
from __future__ import annotations

import uuid
from decimal import Decimal

from aitos.spot.binance import BinanceSpotAdapter
from aitos.spot.filters import SpotSymbolFilters
from aitos.spot.models import SpotOrder, SpotOrderRequest, SpotOrderType


class SpotExecutionDisabled(RuntimeError):
    pass


class SpotOrderExecutor:
    def __init__(self, adapter: BinanceSpotAdapter, *,
                 enabled: bool = False, allow_mainnet: bool = False,
                 symbol_filters: dict[str, SpotSymbolFilters] | None = None) -> None:
        self.adapter = adapter
        self.enabled = enabled
        self.allow_mainnet = allow_mainnet
        self.filters = dict(symbol_filters or {})

    async def submit(self, request: SpotOrderRequest) -> SpotOrder:
        if not self.enabled:
            raise SpotExecutionDisabled("Spot execution is disabled; explicitly enable it after approval")
        if not self.adapter.testnet and not self.allow_mainnet:
            raise SpotExecutionDisabled("mainnet Spot orders require allow_mainnet=True")
        if not request.client_order_id:
            request = SpotOrderRequest(
                symbol=request.symbol, side=request.side, order_type=request.order_type,
                quantity=request.quantity, quote_order_quantity=request.quote_order_quantity,
                price=request.price, time_in_force=request.time_in_force,
                client_order_id="aitos-" + uuid.uuid4().hex[:28],
            )
        filters = self.filters.get(request.symbol)
        if filters is None:
            filters = (await self.adapter.exchange_info()).get(request.symbol)
        if filters is None or filters.status != "TRADING" or not filters.is_spot_trading_allowed:
            raise ValueError("symbol is not enabled for Spot trading")
        if request.client_order_id and len(request.client_order_id) > 36:
            raise ValueError("client_order_id must not exceed 36 characters")
        if request.quote_order_quantity is not None:
            if request.order_type is not SpotOrderType.MARKET or not filters.quote_order_qty_market_allowed:
                raise ValueError("quoteOrderQty is not supported for this symbol/order type")
            if filters.min_notional > 0 and request.quote_order_quantity < filters.min_notional:
                raise ValueError("quoteOrderQty violates minimum notional")
            if filters.max_notional and filters.max_notional > 0 and request.quote_order_quantity > filters.max_notional:
                raise ValueError("quoteOrderQty exceeds maximum notional")
        elif request.quantity is not None:
            price = request.price
            if price is None:
                ticker = await self.adapter._request("GET", "/api/v3/ticker/price",
                                                      params={"symbol": request.symbol})
                price = Decimal(str(ticker["price"]))
            filters.validate(request.quantity, price, market=request.order_type is SpotOrderType.MARKET)
        return await self.adapter.place_order(request)
