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
    ) -> Decimal | None:
        rows = await self.adapter.agg_trades(
            symbol,
            start_time_ms=max(0, fill_time_ms - self.max_price_age_ms),
            end_time_ms=fill_time_ms,
            limit=1000,
        )
        eligible = [
            row for row in rows
            if int(row.get("T", -1)) <= fill_time_ms
            and int(row.get("T", -1)) >= fill_time_ms - self.max_price_age_ms
        ]
        if not eligible:
            return None
        latest = max(eligible, key=lambda row: int(row["T"]))
        price = Decimal(str(latest["p"]))
        return price if price > 0 else None

    async def resolve_pending(self) -> dict[str, int]:
        filters = await self.adapter.exchange_info()
        resolved = skipped = 0
        for item in self.inventory.pending_fee_conversions():
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
            if direct in filters:
                price = await self._historical_price(direct, int(fill_time))
                if price is not None:
                    conversion = commission * price
            elif inverse in filters:
                price = await self._historical_price(inverse, int(fill_time))
                if price is not None:
                    conversion = commission / price
            if conversion is None or conversion <= 0:
                skipped += 1
                continue
            self.inventory.resolve_commission_conversion(
                str(item["fill_key"]), conversion
            )
            resolved += 1
        return {"resolved": resolved, "pending": skipped}
