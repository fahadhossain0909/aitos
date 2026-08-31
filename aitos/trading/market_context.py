"""Market context provider for live Exit Intelligence.

Bridges LiveMarketStateStore into the kwargs that TradeLifecycle.update_price
/ PositionManager.evaluate expect. Without a provider, EIE runs price-only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from aitos.intelligence.liquidity_tracker import LiquidityEvent
from aitos.intelligence.order_flow_engine import OrderFlowFeatures
from aitos.logging_setup import get_logger

logger = get_logger("aitos.trading.market_context")


@dataclass(frozen=True)
class MarketContext:
    order_flow: OrderFlowFeatures | None = None
    liquidity_events: tuple[LiquidityEvent, ...] = ()
    prior_highs: tuple[float, ...] = ()
    prior_lows: tuple[float, ...] = ()
    swing_highs: tuple[float, ...] = ()
    swing_lows: tuple[float, ...] = ()
    structure_break_level: float | None = None
    atr: float | None = None
    trend_strength: float | None = None
    volume_profile: Any = None
    extra_features: dict[str, float] = field(default_factory=dict)
    source: str = "none"

    def as_kwargs(self) -> dict[str, Any]:
        return {
            "order_flow": self.order_flow,
            "volume_profile": self.volume_profile,
            "liquidity_events": self.liquidity_events,
            "prior_highs": self.prior_highs,
            "prior_lows": self.prior_lows,
            "swing_highs": self.swing_highs,
            "swing_lows": self.swing_lows,
            "structure_break_level": self.structure_break_level,
            "atr": self.atr,
            "trend_strength": self.trend_strength,
            "extra_features": self.extra_features or None,
        }


class MarketContextProvider(Protocol):
    def get_context(self, symbol: str) -> MarketContext: ...


class LiveStateContextProvider:
    def __init__(self, store: Any) -> None:
        self._store = store

    def get_context(self, symbol: str) -> MarketContext:
        try:
            snap = self._store.snapshot_model(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.debug("live state context unavailable: %s", exc)
            return MarketContext(source="live_state_error")
        return MarketContext(
            order_flow=snap.order_flow,
            liquidity_events=snap.liquidity_events,
            source="live_state",
        )


class CompositeContextProvider:
    def __init__(self, *providers: MarketContextProvider) -> None:
        self._providers = list(providers)

    def get_context(self, symbol: str) -> MarketContext:
        of = None
        events: list[LiquidityEvent] = []
        prior_h: list[float] = []
        prior_l: list[float] = []
        swing_h: list[float] = []
        swing_l: list[float] = []
        structure_break = None
        atr = None
        trend = None
        vp = None
        extra: dict[str, float] = {}
        sources: list[str] = []

        for p in self._providers:
            try:
                ctx = p.get_context(symbol)
            except Exception:  # noqa: BLE001
                continue
            sources.append(ctx.source)
            if ctx.order_flow is not None:
                of = ctx.order_flow
            if ctx.liquidity_events:
                events.extend(ctx.liquidity_events)
            if ctx.prior_highs:
                prior_h.extend(ctx.prior_highs)
            if ctx.prior_lows:
                prior_l.extend(ctx.prior_lows)
            if ctx.swing_highs:
                swing_h.extend(ctx.swing_highs)
            if ctx.swing_lows:
                swing_l.extend(ctx.swing_lows)
            if ctx.structure_break_level is not None:
                structure_break = ctx.structure_break_level
            if ctx.atr is not None:
                atr = ctx.atr
            if ctx.trend_strength is not None:
                trend = ctx.trend_strength
            if ctx.volume_profile is not None:
                vp = ctx.volume_profile
            extra.update(ctx.extra_features)

        return MarketContext(
            order_flow=of,
            liquidity_events=tuple(events),
            prior_highs=tuple(prior_h),
            prior_lows=tuple(prior_l),
            swing_highs=tuple(swing_h),
            swing_lows=tuple(swing_l),
            structure_break_level=structure_break,
            atr=atr,
            trend_strength=trend,
            volume_profile=vp,
            extra_features=extra,
            source="+".join(sources) if sources else "empty",
        )


class CallableContextProvider:
    def __init__(self, fn: Callable[[str], MarketContext]) -> None:
        self._fn = fn

    def get_context(self, symbol: str) -> MarketContext:
        return self._fn(symbol)
