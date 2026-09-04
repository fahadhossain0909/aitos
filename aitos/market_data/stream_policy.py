"""Adaptive subscription policy for the canonical market-data gateway."""

from __future__ import annotations

from dataclasses import dataclass


# Historical deep-book capture is intentionally fixed so historical storage
# remains stable across live-ranking changes and replay datasets remain
# comparable over time.
HISTORICAL_DEEP_SYMBOLS: tuple[str, ...] = ("BTCUSDT", "LTCUSDT")


@dataclass(frozen=True, slots=True)
class SubscriptionPlan:
    universe: tuple[str, ...]
    candidate25: tuple[str, ...]
    candidate10: tuple[str, ...]
    candidate5: tuple[str, ...]
    deep: tuple[str, ...]
    historical_book: tuple[str, ...]


def build_subscription_plan(
    ranked_symbols: list[str] | tuple[str, ...], *, btc_symbol: str = "BTCUSDT"
) -> SubscriptionPlan:
    """Build the ranked universe and promote BTC plus the strongest two others.

    Live deep analysis follows the ranked promotion policy, while historical
    deep-book capture uses the fixed symbol set defined by
    ``HISTORICAL_DEEP_SYMBOLS`` so ranking changes do not alter the historical
    dataset contract.
    """
    universe = tuple(dict.fromkeys(s.upper() for s in ranked_symbols if s))
    candidate25 = universe[:25]
    candidate10 = candidate25[:10]
    candidate5 = candidate10[:5]
    btc = btc_symbol.upper()
    non_btc = tuple(s for s in universe if s != btc)
    deep = tuple(dict.fromkeys((btc, *non_btc[:2])))
    return SubscriptionPlan(
        universe=universe,
        candidate25=candidate25,
        candidate10=candidate10,
        candidate5=candidate5,
        deep=deep,
        historical_book=HISTORICAL_DEEP_SYMBOLS,
    )
