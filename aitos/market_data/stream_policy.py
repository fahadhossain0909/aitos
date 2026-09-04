"""Adaptive subscription policy for the canonical market-data gateway."""

from __future__ import annotations

from dataclasses import dataclass


DEEP_HISTORICAL_SYMBOLS = ("BTCUSDT", "LTCUSDT")


@dataclass(frozen=True, slots=True)
class SubscriptionPlan:
    universe: tuple[str, ...]
    candidate25: tuple[str, ...]
    candidate10: tuple[str, ...]
    candidate5: tuple[str, ...]
    deep: tuple[str, ...] = DEEP_HISTORICAL_SYMBOLS
    historical_book: tuple[str, ...] = DEEP_HISTORICAL_SYMBOLS


def build_subscription_plan(
    ranked_symbols: list[str] | tuple[str, ...], *, btc_symbol: str = "BTCUSDT"
) -> SubscriptionPlan:
    """Build ALL -> 25 -> 10 -> 5 while reserving deep history for BTC/LTC.

    The ranking policy controls live candidate selection. Historical deep order
    book capture is deliberately independent of ranking so BTC/LTC remain
    continuously reconstructable for lead/lag research and replay.
    """
    universe = tuple(dict.fromkeys(s.upper() for s in ranked_symbols))
    candidate25 = universe[:25]
    candidate10 = candidate25[:10]
    candidate5 = candidate10[:5]
    deep = tuple(dict.fromkeys((*DEEP_HISTORICAL_SYMBOLS, btc_symbol.upper())))[:2]
    return SubscriptionPlan(
        universe=universe,
        candidate25=candidate25,
        candidate10=candidate10,
        candidate5=candidate5,
        deep=deep,
        historical_book=DEEP_HISTORICAL_SYMBOLS,
    )
