"""Per-symbol trading filters from Binance's ``/fapi/v1/exchangeInfo`` —
``LOT_SIZE`` (quantity step), ``PRICE_FILTER`` (price tick), and
``MIN_NOTIONAL``. Every live order must respect these or Binance rejects
it outright; this is what lets the executor round correctly and refuse a
too-small order locally instead of burning a round-trip on a guaranteed
rejection.

Uses ``Decimal`` throughout — step sizes like ``0.0001`` aren't exactly
representable in binary floating point, and naive float rounding can
round the wrong way right at a boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


def _round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    step_dec = Decimal(str(step))
    value_dec = Decimal(str(value))
    steps = (value_dec / step_dec).to_integral_value(rounding=ROUND_DOWN)
    return float(steps * step_dec)


@dataclass(frozen=True)
class SymbolFilters:
    symbol: str
    step_size: float  # LOT_SIZE
    tick_size: float  # PRICE_FILTER
    min_notional: float  # MIN_NOTIONAL
    quantity_precision: int
    price_precision: int

    def round_quantity(self, quantity: float) -> float:
        """Round down to the nearest valid step — never rounds up, so this
        never accidentally orders more than requested."""
        return _round_to_step(quantity, self.step_size)

    def round_price(self, price: float) -> float:
        return _round_to_step(price, self.tick_size)

    def meets_min_notional(self, price: float, quantity: float) -> bool:
        return price * quantity >= self.min_notional


def parse_exchange_info(raw: dict) -> dict[str, SymbolFilters]:
    """Parse a ``/fapi/v1/exchangeInfo`` response into ``{symbol: SymbolFilters}``."""
    result: dict[str, SymbolFilters] = {}
    for symbol_info in raw.get("symbols", []):
        symbol = symbol_info["symbol"]
        step_size = 0.0
        tick_size = 0.0
        min_notional = 0.0
        for f in symbol_info.get("filters", []):
            filter_type = f.get("filterType")
            if filter_type == "LOT_SIZE":
                step_size = float(f["stepSize"])
            elif filter_type == "PRICE_FILTER":
                tick_size = float(f["tickSize"])
            elif filter_type == "MIN_NOTIONAL":
                min_notional = float(f.get("notional", f.get("minNotional", 0.0)))

        result[symbol] = SymbolFilters(
            symbol=symbol,
            step_size=step_size,
            tick_size=tick_size,
            min_notional=min_notional,
            quantity_precision=int(symbol_info.get("quantityPrecision", 0)),
            price_precision=int(symbol_info.get("pricePrecision", 0)),
        )
    return result
