"""Adaptive subscription policy for the canonical market-data gateway."""

from __future__ import annotations

from dataclasses import dataclass

HISTORICAL_DEEP_SYMBOLS = ("BTCUSDT", "LTCUSDT")


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
    """Build the full ranked universe and promote BTC plus the best two others.

    Live expensive analysis is dynamic: BTC is always retained as the anchor and
    the strongest two non-BTC ranked symbols are promoted into the live deep tier.
    Historical deep-order-book collection is intentionally independent of live
    ranking and remains fixed to BTCUSDT/LTCUSDT for reproducible research data.
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
