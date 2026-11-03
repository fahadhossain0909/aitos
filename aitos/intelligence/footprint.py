"""Price-level executed-trade footprint aggregation.

Aggregates TradeTick records into bid/ask volume per price bucket. This is a
trade-based footprint, not a full exchange order-book footprint: it represents
executed aggression and does not infer hidden/iceberg liquidity.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from aitos.models.market import TradeSide, TradeTick


@dataclass(frozen=True)
class FootprintLevel:
    price: float
    bid_volume: float
    ask_volume: float

    @property
    def total_volume(self) -> float:
        return self.bid_volume + self.ask_volume

    @property
    def delta(self) -> float:
        return self.ask_volume - self.bid_volume

    @property
    def imbalance(self) -> float:
        total = self.total_volume
        return self.delta / total if total else 0.0


@dataclass(frozen=True)
class Footprint:
    symbol: str
    start_time: datetime
    end_time: datetime
    tick_size: float
    levels: tuple[FootprintLevel, ...]

    @property
    def total_volume(self) -> float:
        return sum(level.total_volume for level in self.levels)

    @property
    def total_delta(self) -> float:
        return sum(level.delta for level in self.levels)

    @property
    def max_delta_level(self) -> FootprintLevel | None:
        return max(self.levels, key=lambda level: abs(level.delta), default=None)


class FootprintEngine:
    """Build deterministic trade footprints from live or historical TradeTicks."""

    def __init__(self, tick_size: float) -> None:
        if tick_size <= 0:
            raise ValueError("tick_size must be positive")
        self.tick_size = tick_size

    def bucket_price(self, price: float) -> float:
        # Avoid binary floating-point drift around price increments.
        bucket = round(round(price / self.tick_size) * self.tick_size, 12)
        return bucket

    def build(self, trades: Iterable[TradeTick]) -> Footprint | None:
        trades = list(trades)
        if not trades:
            return None
        symbol = trades[0].symbol
        if any(trade.symbol != symbol for trade in trades):
            raise ValueError("all trades must belong to the same symbol")

        buckets: dict[float, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for trade in trades:
            price = self.bucket_price(trade.price)
            # Buyer-maker means the taker is selling; otherwise the taker is buying.
            if trade.is_buyer_maker or trade.side == TradeSide.SELL:
                buckets[price][0] += trade.quantity
            else:
                buckets[price][1] += trade.quantity

        levels = tuple(
            FootprintLevel(price, values[0], values[1])
            for price, values in sorted(buckets.items())
        )
        return Footprint(
            symbol=symbol,
            start_time=min(trade.timestamp for trade in trades),
            end_time=max(trade.timestamp for trade in trades),
            tick_size=self.tick_size,
            levels=levels,
        )
