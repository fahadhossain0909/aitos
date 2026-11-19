"""Point-in-time conversion of third-asset Spot commissions into quote currency."""

from __future__ import annotations

from decimal import Decimal

from aitos.spot.binance import BinanceSpotAdapter
from aitos.spot.inventory import InventoryLedger


class SpotCommissionConversionService:
    """Resolve pending fees only from historical Binance Spot trade prints.

    The latest aggregate trade at or before the fill timestamp is used, with a
    strict age bound. Missing pairs, timestamps or historical prints remain
    pending; the service never substitutes a current ticker price.
    """

    def __init__(
        self,
        *,
        adapter: BinanceSpotAdapter,
        inventory: InventoryLedger,
        max_price_age_ms: int = 5000,
    ) -> None:
        if max_price_age_ms <= 0:
            raise ValueError("max_price_age_ms must be positive")
        self.adapter = adapter
        self.inventory = inventory
        self.max_price_age_ms = max_price_age_ms

    async def _historical_price(
        self, symbol: str, fill_time_ms: int
    ) -> dict[str, Decimal | int] | None:
        rows = await self.adapter.agg_trades(
            symbol,
            start_time_ms=max(0, fill_time_ms - self.max_price_age_ms),
            end_time_ms=fill_time_ms,
            limit=1000,
        )
        eligible = [
            row
            for row in rows
            if int(row.get("T", -1)) <= fill_time_ms
            and int(row.get("T", -1)) >= fill_time_ms - self.max_price_age_ms
        ]
        if not eligible:
            return None
        latest = max(eligible, key=lambda row: int(row["T"]))
        price = Decimal(str(latest["p"]))
        if price <= 0:
            return None
        return {
            "price": price,
            "trade_id": int(latest["a"]) if latest.get("a") is not None else -1,
            "time_ms": int(latest["T"]),
        }

    async def resolve_pending(self) -> dict[str, int]:
        pending_items = self.inventory.pending_fee_conversions()
        if not pending_items:
            return {"resolved": 0, "pending": 0}
        filters = await self.adapter.exchange_info()
        resolved = skipped = 0
        for item in pending_items:
            fill_time = item.get("fill_time_ms")
            if fill_time is None:
                skipped += 1
                continue
            fee_asset = str(item["commission_asset"]).upper()
            quote_asset = str(item["quote_asset"]).upper()
            commission = Decimal(str(item["commission"]))
            direct = f"{fee_asset}{quote_asset}"
            inverse = f"{quote_asset}{fee_asset}"
            conversion = None
            direct_filter = filters.get(direct)
            inverse_filter = filters.get(inverse)
            if (
                direct_filter is not None
                and direct_filter.status == "TRADING"
                and direct_filter.is_spot_trading_allowed
            ):
                historical = await self._historical_price(direct, int(fill_time))
                if historical is not None:
                    price = historical["price"]
                    conversion = commission * price
                    conversion_symbol = direct
            if (
                conversion is None
                and inverse_filter is not None
                and inverse_filter.status == "TRADING"
                and inverse_filter.is_spot_trading_allowed
            ):
                historical = await self._historical_price(inverse, int(fill_time))
                if historical is not None:
                    price = historical["price"]
                    conversion = commission / price
                    conversion_symbol = inverse
            if conversion is None or conversion <= 0:
                skipped += 1
                continue
            self.inventory.resolve_commission_conversion(
                str(item["fill_key"]),
                conversion,
                conversion_symbol=conversion_symbol,
                conversion_price=price,
                conversion_trade_id=(
                    int(historical["trade_id"])
                    if int(historical["trade_id"]) >= 0
                    else None
                ),
                conversion_time_ms=int(historical["time_ms"]),
                conversion_source="binance_spot_aggTrades",
            )
            resolved += 1
        return {"resolved": resolved, "pending": skipped}
