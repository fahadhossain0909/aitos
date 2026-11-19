"""Binance Spot exchangeInfo filters with exact Decimal arithmetic."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any


def D(value: Any) -> Decimal:
    return Decimal(str(value))


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    if value < 0:
        raise ValueError("value cannot be negative")
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass(frozen=True, slots=True)
class SpotSymbolFilters:
    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    lot_step_size: Decimal
    lot_min_qty: Decimal
    lot_max_qty: Decimal
    market_step_size: Decimal
    market_min_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal
    max_notional: Decimal | None = None
    apply_min_to_market: bool = True
    apply_max_to_market: bool = False
    quote_order_qty_market_allowed: bool = False
    is_spot_trading_allowed: bool = False

    def round_price(self, price: Decimal) -> Decimal:
        return floor_step(price, self.tick_size)

    def round_quantity(self, quantity: Decimal, market: bool = False) -> Decimal:
        step = self.market_step_size if market else self.lot_step_size
        return floor_step(quantity, step)

    def validate(
        self, quantity: Decimal, price: Decimal, market: bool = False
    ) -> Decimal:
        if quantity <= 0 or price <= 0:
            raise ValueError("quantity and price must be positive")
        if not market:
            if self.min_price > 0 and price < self.min_price:
                raise ValueError("price is below PRICE_FILTER minimum")
            if self.max_price > 0 and price > self.max_price:
                raise ValueError("price exceeds PRICE_FILTER maximum")
            if self.tick_size > 0 and self.round_price(price) != price:
                raise ValueError("price is not aligned to PRICE_FILTER tick size")
        step = self.market_step_size if market else self.lot_step_size
        minimum = self.market_min_qty if market else self.lot_min_qty
        maximum = self.market_max_qty if market else self.lot_max_qty
        rounded = self.round_quantity(quantity, market=market)
        if rounded < minimum or (maximum > 0 and rounded > maximum):
            raise ValueError("quantity violates LOT_SIZE/MARKET_LOT_SIZE")
        if step > 0 and rounded != quantity:
            raise ValueError("quantity is not aligned to exchange step size")
        notional = rounded * price
        if self.min_notional > 0 and (not market or self.apply_min_to_market):
            if notional < self.min_notional:
                raise ValueError("order violates minimum notional")
        if self.max_notional and self.max_notional > 0:
            if (
                not market or self.apply_max_to_market
            ) and notional > self.max_notional:
                raise ValueError("order exceeds maximum notional")
        return rounded


def parse_spot_exchange_info(payload: dict[str, Any]) -> dict[str, SpotSymbolFilters]:
    """Parse /api/v3/exchangeInfo. Unsupported/disabled symbols are retained."""
    result: dict[str, SpotSymbolFilters] = {}
    for item in payload.get("symbols", []):
        filters = {f.get("filterType"): f for f in item.get("filters", [])}
        price = filters.get("PRICE_FILTER", {})
        lot = filters.get("LOT_SIZE", {})
        market_lot = filters.get("MARKET_LOT_SIZE", lot)
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        min_notional = D(notional.get("minNotional", 0))
        result[item["symbol"]] = SpotSymbolFilters(
            symbol=item["symbol"],
            status=str(item.get("status", "UNKNOWN")).upper(),
            base_asset=item.get("baseAsset", "").upper(),
            quote_asset=item.get("quoteAsset", "").upper(),
            tick_size=D(price.get("tickSize", 0)),
            min_price=D(price.get("minPrice", 0)),
            max_price=D(price.get("maxPrice", 0)),
            lot_step_size=D(lot.get("stepSize", 0)),
            lot_min_qty=D(lot.get("minQty", 0)),
            lot_max_qty=D(lot.get("maxQty", 0)),
            market_step_size=D(market_lot.get("stepSize", 0)),
            market_min_qty=D(market_lot.get("minQty", 0)),
            market_max_qty=D(market_lot.get("maxQty", 0)),
            min_notional=min_notional,
            max_notional=(
                D(notional["maxNotional"]) if notional.get("maxNotional") else None
            ),
            apply_min_to_market=bool(
                notional.get("applyToMarket", notional.get("applyMinToMarket", True))
            ),
            apply_max_to_market=bool(notional.get("applyMaxToMarket", False)),
            quote_order_qty_market_allowed=bool(
                item.get("quoteOrderQtyMarketAllowed", False)
            ),
            is_spot_trading_allowed=bool(item.get("isSpotTradingAllowed", False)),
        )
    return result
