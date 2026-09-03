"""Adaptive subscription policy for the market-data gateway.

The policy is intentionally deterministic: ranking decides which symbols are
promoted; transport adapters decide how the corresponding Binance streams are
opened. This keeps universe selection separate from exchange protocol code.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SubscriptionPlan:
    universe: tuple[str, ...]
    candidate25: tuple[str, ...]
    candidate10: tuple[str, ...]
    candidate5: tuple[str, ...]
    deep: tuple[str, ...]
    historical_book: tuple[str, ...] = ("BTCUSDT", "LTCUSDT")


def build_subscription_plan(
    ranked_symbols: list[str] | tuple[str, ...], *, btc_symbol: str = "BTCUSDT"
) -> SubscriptionPlan:
    """Build the ALL -> 25 -> 10 -> 5 -> 2 promotion plan.

    BTC is always deep. The two highest-ranked non-BTC symbols are promoted
    alongside BTC. Duplicates are removed while preserving ranking order.
    """
    universe = tuple(dict.fromkeys(ranked_symbols))
    candidate25 = universe[:25]
    candidate10 = candidate25[:10]
    candidate5 = candidate10[:5]
    non_btc = [symbol for symbol in candidate5 if symbol != btc_symbol]
    non_btc.extend(symbol for symbol in universe[5:] if symbol != btc_symbol)
    deep_non_btc = tuple(dict.fromkeys(non_btc))[:2]
    deep = (btc_symbol, *deep_non_btc)
    return SubscriptionPlan(
        universe=universe,
        candidate25=candidate25,
        candidate10=candidate10,
        candidate5=candidate5,
        deep=deep,
    )
