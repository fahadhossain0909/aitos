"""Volume-at-price and value-area calculations for AMT.

The implementation accepts executed trades, bins them by a configured price
step, finds POC, then expands around POC until the requested fraction of total
volume is represented. It intentionally does not pretend candle volume is
volume-at-price.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import floor

from aitos.models.market import TradeTick


@dataclass(frozen=True)
class VolumeProfile:
    bins: tuple[tuple[float, float], ...]
    poc: float
    vah: float
    val: float
    high: float
    low: float
    total_volume: float
    value_area_volume: float
    value_area_pct: float

    @property
    def hvn(self) -> tuple[float, ...]:
        if not self.bins:
            return ()
        volumes = [v for _, v in self.bins]
        threshold = sum(volumes) / len(volumes)
        return tuple(p for p, v in self.bins if v >= threshold)

    @property
    def lvn(self) -> tuple[float, ...]:
        if not self.bins:
            return ()
        volumes = [v for _, v in self.bins]
        threshold = sum(volumes) / len(volumes)
        return tuple(p for p, v in self.bins if v < threshold)


def _bin_price(price: float, step: float) -> float:
    return floor(price / step + 1e-12) * step


def build_volume_profile(
    trades: Iterable[TradeTick],
    tick_size: float,
    value_area_pct: float = 0.70,
) -> VolumeProfile:
    """Build a deterministic volume profile from executed trades.

    ``tick_size`` is a price bin size, not necessarily the exchange's minimum
    order tick. Callers may choose a larger aggregation step for noisy markets.
    """
    if tick_size <= 0:
        raise ValueError("tick_size must be > 0")
    if not 0 < value_area_pct <= 1:
        raise ValueError("value_area_pct must be in (0, 1]")

    volume_by_price: dict[float, float] = {}
    for trade in trades:
        if trade.price <= 0 or trade.quantity <= 0:
            continue
        price = _bin_price(trade.price, tick_size)
        volume_by_price[price] = volume_by_price.get(price, 0.0) + trade.quantity

    if not volume_by_price:
        return VolumeProfile((), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, value_area_pct)

    ordered = sorted(volume_by_price.items())
    total = sum(v for _, v in ordered)
    poc_index = max(range(len(ordered)), key=lambda i: (ordered[i][1], -ordered[i][0]))

    target = total * value_area_pct
    included = {poc_index}
    covered = ordered[poc_index][1]
    left = poc_index - 1
    right = poc_index + 1
    while covered < target and (left >= 0 or right < len(ordered)):
        left_volume = ordered[left][1] if left >= 0 else -1.0
        right_volume = ordered[right][1] if right < len(ordered) else -1.0
        if right_volume > left_volume:
            included.add(right)
            covered += right_volume
            right += 1
        else:
            included.add(left)
            covered += left_volume
            left -= 1

    prices = [ordered[i][0] for i in included]
    return VolumeProfile(
        bins=tuple(ordered),
        poc=ordered[poc_index][0],
        vah=max(prices),
        val=min(prices),
        high=ordered[-1][0],
        low=ordered[0][0],
        total_volume=total,
        value_area_volume=covered,
        value_area_pct=covered / total if total else 0.0,
    )
