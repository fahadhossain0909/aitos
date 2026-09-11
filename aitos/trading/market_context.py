"""Market context provider for live Exit Intelligence.

Bridges LiveMarketStateStore into the kwargs that TradeLifecycle.update_price
/ PositionManager.evaluate expect. Without a provider, EIE runs price-only.

This module also owns the canonical runtime bridge from live market-price
EventBus messages to open-position ``TradeLifecycle.update_price`` calls.
The bridge is installed at the EventBus subscription boundary so the exact
callback consumed by Redis is deterministic; this avoids relying on a class
method monkey-patch being observed by an already-bound callback.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from aitos.core.contracts import Event
from aitos.intelligence.liquidity_tracker import LiquidityEvent
from aitos.intelligence.order_flow_engine import OrderFlowFeatures
from aitos.logging_setup import get_logger

logger = get_logger("aitos.trading.market_context")
_BRIDGE_DELIVERIES = 0


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


async def handle_position_market_event(
    trade_lifecycle: Any,
    provider: MarketContextProvider | None,
    event: Event,
) -> bool:
    """Deliver a live price event to every open position for that symbol.

    Returns ``True`` when a market-price event has been consumed by this
    canonical position bridge. This prevents a second native price path from
    racing the canonical ``update_price`` call.
    """
    global _BRIDGE_DELIVERIES

    if not (
        event.topic.startswith("market.kline.")
        or event.topic.startswith("market.trade.")
    ):
        return False

    symbol = event.payload.get("symbol")
    price = event.payload.get("close", event.payload.get("price"))
    from aitos.trading.lifecycle import _valid_market_price

    if not symbol or not _valid_market_price(price):
        return True

    symbol = str(symbol).upper()
    current_price = float(price)
    matching_trades = [
        trade
        for trade in list(trade_lifecycle.get_open_trades())
        if str(getattr(trade, "symbol", "")).upper() == symbol
    ]
    if not matching_trades:
        return True

    try:
        ctx_kwargs = provider.get_context(symbol).as_kwargs() if provider else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("position market context unavailable: %s", exc)
        ctx_kwargs = {}

    for trade in matching_trades:
        await trade_lifecycle.update_price(trade.trade_id, current_price, **ctx_kwargs)
        _BRIDGE_DELIVERIES += 1
        if _BRIDGE_DELIVERIES == 1 or _BRIDGE_DELIVERIES % 100 == 0:
            logger.info(
                "POSITION_MONITOR market bridge delivered live price",
                extra={
                    "aitos_extra": {
                        "deliveries": _BRIDGE_DELIVERIES,
                        "trade_id": trade.trade_id,
                        "symbol": symbol,
                        "price": current_price,
                        "topic": event.topic,
                        "matched_positions": len(matching_trades),
                    }
                },
            )
    return True


def install_trade_lifecycle_market_bridge() -> None:
    """Install a canonical market bridge at both lifecycle and EventBus edges.

    The lifecycle wrapper preserves compatibility for direct callers. The
    EventBus wrapper is the production-critical path: it replaces the exact
    bound ``TradeLifecycle.handle_event`` callback supplied to
    ``EventBus.subscribe`` before Redis consumers are created.
    """
    from aitos.eventbus.redis_bus import EventBus
    from aitos.trading.lifecycle import TradeLifecycle

    if not getattr(TradeLifecycle, "_aitos_canonical_market_bridge_installed", False):
        original_handle_event = TradeLifecycle.handle_event

        async def _canonical_handle_event(self: Any, event: Event):
            provider = getattr(self, "market_context_provider", None)
            if await handle_position_market_event(self, provider, event):
                return None
            return await original_handle_event(self, event)

        TradeLifecycle.handle_event = _canonical_handle_event
        TradeLifecycle._aitos_canonical_market_bridge_installed = True

    if getattr(EventBus, "_aitos_canonical_position_subscription_installed", False):
        return

    original_subscribe = EventBus.subscribe

    async def _canonical_subscribe(
        self: Any,
        topic: str,
        handler: Callable[..., Any],
        group: str = "default",
        start_id: str = "0",
    ):
        lifecycle = getattr(handler, "__self__", None)
        is_position_lifecycle = (
            lifecycle is not None
            and hasattr(lifecycle, "get_open_trades")
            and hasattr(lifecycle, "update_price")
            and hasattr(lifecycle, "market_context_provider")
        )
        is_market_topic = topic.startswith("market.kline.") or topic.startswith(
            "market.trade."
        )
        if is_position_lifecycle and is_market_topic:

            async def _position_market_handler(event: Event):
                provider = getattr(lifecycle, "market_context_provider", None)
                return await handle_position_market_event(lifecycle, provider, event)

            handler = _position_market_handler
            logger.info(
                "Canonical position market handler bound to EventBus subscription",
                extra={
                    "aitos_extra": {
                        "topic": topic,
                        "group": group,
                    }
                },
            )
        return await original_subscribe(
            self, topic, handler, group=group, start_id=start_id
        )

    EventBus.subscribe = _canonical_subscribe  # type: ignore[method-assign]
    EventBus._aitos_canonical_position_subscription_installed = True  # type: ignore[attr-defined]
    logger.info("Canonical position market-event bridge installed")


install_trade_lifecycle_market_bridge()
