"""Quote-currency portfolio valuation for separately held Spot and Futures accounts.

Spot inventory is sourced only from AITOS-managed fills. Exchange base-asset
wallet balances are intentionally not adopted as strategy inventory.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from aitos.spot.binance import BinanceSpotAdapter
from aitos.spot.inventory import InventoryLedger


@dataclass(frozen=True, slots=True)
class SpotPortfolioSnapshot:
    quote_asset: str
    futures_account_equity_quote: Decimal
    spot_quote_cash: Decimal
    managed_inventory_market_value: Decimal
    managed_inventory_cost_basis: Decimal
    total_equity_quote: Decimal
    pending_third_asset_fee_conversions: int
    marks_missing: tuple[str, ...]

    @property
    def accounting_complete(self) -> bool:
        return not self.marks_missing and self.pending_third_asset_fee_conversions == 0


class SpotPortfolioValuator:
    """Aggregate isolated Spot wallet and Futures equity without quote duplication.

    futures_account_equity_quote must come from the Futures account only; spot
    quote cash must come from the separate Spot account. Do not pass a combined
    cross-account aggregate into either input.
    """

    def __init__(self, inventory: InventoryLedger, quote_asset: str = "USDT") -> None:
        self.inventory = inventory
        self.quote_asset = quote_asset.upper()

    async def snapshot(
        self,
        *,
        spot_adapter: BinanceSpotAdapter,
        futures_account_equity_quote: Decimal,
        marks: Mapping[str, Decimal],
        balances: Sequence | None = None,
    ) -> SpotPortfolioSnapshot:
        if futures_account_equity_quote < 0:
            raise ValueError("Futures account equity cannot be negative")
        wallet = list(balances) if balances is not None else await spot_adapter.account()
        quote_cash = sum(
            (balance.total for balance in wallet if balance.asset == self.quote_asset),
            Decimal("0"),
        )
        inventory_value = Decimal("0")
        inventory_cost = Decimal("0")
        missing: list[str] = []
        symbols = sorted({str(item["symbol"]) for item in self.inventory.managed_symbols()})
        for symbol in symbols:
            position = self.inventory.get_position(symbol)
            if position is None or position.quantity <= 0:
                continue
            if position.quote_asset != self.quote_asset:
                raise ValueError(
                    f"{symbol} uses {position.quote_asset}; FX conversion is not configured"
                )
            mark = marks.get(symbol)
            if mark is None or mark <= 0:
                missing.append(symbol)
                continue
            inventory_value += position.quantity * mark
            inventory_cost += position.cost_quote
        pending = len(self.inventory.pending_fee_conversions())
        total = futures_account_equity_quote + quote_cash + inventory_value
        return SpotPortfolioSnapshot(
            quote_asset=self.quote_asset,
            futures_account_equity_quote=futures_account_equity_quote,
            spot_quote_cash=quote_cash,
            managed_inventory_market_value=inventory_value,
            managed_inventory_cost_basis=inventory_cost,
            total_equity_quote=total,
            pending_third_asset_fee_conversions=pending,
            marks_missing=tuple(missing),
        )
