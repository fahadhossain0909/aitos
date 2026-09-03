"""Adaptive market-universe policy.

This module contains policy only; it does not fetch data or perform expensive
calculations. Keeping ranking separate from transport prevents scanner load
from feeding back into the market-data gateway.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class ScanTier(IntEnum):
    UNIVERSE = 0
    TOP_25 = 1
    TOP_10 = 2
    TOP_5 = 3
    TOP_2 = 4
    ANCHOR = 5


@dataclass(frozen=True, slots=True)
class ScanLimits:
    top_25: int = 25
    top_10: int = 10
    top_5: int = 5
    top_2: int = 2

    def __post_init__(self) -> None:
        if not (0 < self.top_2 <= self.top_5 <= self.top_10 <= self.top_25):
            raise ValueError("scan limits must satisfy 0 < top_2 <= top_5 <= top_10 <= top_25")


@dataclass(frozen=True, slots=True)
class InstrumentScore:
    symbol: str
    score: float
    tier: ScanTier = ScanTier.UNIVERSE


def promote(ranked: list[InstrumentScore], limits: ScanLimits | None = None) -> dict[ScanTier, list[str]]:
    """Return deterministic candidate tiers from an already-ranked universe."""
    limits = limits or ScanLimits()
    ordered = sorted(ranked, key=lambda item: (-item.score, item.symbol))
    return {
        ScanTier.UNIVERSE: [item.symbol for item in ordered],
        ScanTier.TOP_25: [item.symbol for item in ordered[: limits.top_25]],
        ScanTier.TOP_10: [item.symbol for item in ordered[: limits.top_10]],
        ScanTier.TOP_5: [item.symbol for item in ordered[: limits.top_5]],
        ScanTier.TOP_2: [item.symbol for item in ordered[: limits.top_2]],
    }


def deep_symbols(ranked: list[InstrumentScore], anchor: str = "BTCUSDT", limits: ScanLimits | None = None) -> list[str]:
    """Select BTC plus the strongest two non-BTC candidates."""
    limits = limits or ScanLimits()
    ordered = sorted(ranked, key=lambda item: (-item.score, item.symbol))
    result = [anchor]
    for item in ordered:
        if item.symbol != anchor and item.symbol not in result:
            result.append(item.symbol)
        if len(result) >= limits.top_2 + 1:
            break
    return result
