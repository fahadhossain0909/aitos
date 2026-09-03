"""Venue-neutral subscription state manager.

This module owns desired stream state only. A transport adapter can apply the
returned delta to an exchange connection. Keeping desired state here prevents
scanner ranking changes from leaking into exchange protocol code.
"""

from __future__ import annotations

from dataclasses import dataclass

from .stream_policy import SubscriptionPlan, build_subscription_plan


@dataclass(frozen=True, slots=True)
class SubscriptionDelta:
    subscribe: tuple[str, ...]
    unsubscribe: tuple[str, ...]


class SubscriptionManager:
    def __init__(self, *, permanent: tuple[str, ...] = ("BTCUSDT",)) -> None:
        self._permanent = tuple(dict.fromkeys(permanent))
        self._active: set[str] = set(self._permanent)

    @property
    def active(self) -> frozenset[str]:
        return frozenset(self._active)

    def apply_ranked_symbols(self, ranked_symbols: list[str] | tuple[str, ...]) -> SubscriptionDelta:
        plan: SubscriptionPlan = build_subscription_plan(
            ranked_symbols, btc_symbol=self._permanent[0]
        )
        desired = set(plan.deep) | set(self._permanent)
        to_subscribe = tuple(sorted(desired - self._active))
        to_unsubscribe = tuple(sorted(self._active - desired))
        self._active.update(to_subscribe)
        self._active.difference_update(to_unsubscribe)
        return SubscriptionDelta(to_subscribe, to_unsubscribe)

    def reset(self) -> SubscriptionDelta:
        desired = set(self._permanent)
        delta = SubscriptionDelta(
            subscribe=tuple(sorted(desired - self._active)),
            unsubscribe=tuple(sorted(self._active - desired)),
        )
        self._active = desired
        return delta
