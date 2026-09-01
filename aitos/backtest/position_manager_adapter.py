"""Bridge historical market state into the canonical PositionManager.

No alternate management policy is implemented here. Existing positions are
managed exclusively by PositionManager; this adapter only reconstructs
historical context and records hedge lifecycle events for replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aitos.backtest.market_adapter import HistoricalMarketAdapter, HistoricalMarketState
from aitos.intelligence.amt.volume_profile import VolumeProfile, build_volume_profile
from aitos.intelligence.order_flow_engine import OrderFlowFeatures
from aitos.models.trade import Trade
from aitos.trading.position_manager import PositionAction, PositionManager


@dataclass(frozen=True)
class HistoricalPositionContext:
    state: HistoricalMarketState
    volume_profile: VolumeProfile | None
    order_flow: OrderFlowFeatures | None
    current_price: float
    timestamp: datetime
    prior_highs: tuple[float, ...] = ()
    prior_lows: tuple[float, ...] = ()
    swing_highs: tuple[float, ...] = ()
    swing_lows: tuple[float, ...] = ()
    atr: float | None = None
    trend_strength: float | None = None
    structure_break_level: float | None = None
    extra_features: dict[str, float] | None = None


class HistoricalPositionManagerAdapter:
    """Evaluate the real PositionManager against historical market state."""

    def __init__(
        self,
        market: HistoricalMarketAdapter,
        position_manager: PositionManager | None = None,
        *,
        value_area_pct: float = 0.70,
        context_trade_window: int = 500,
    ) -> None:
        self.market = market
        self.position_manager = position_manager or PositionManager()
        self.value_area_pct = value_area_pct
        self.context_trade_window = context_trade_window

    def _volume_profile(self) -> VolumeProfile | None:
        trades = self.market.order_flow.trades
        if not trades:
            return None
        return build_volume_profile(
            trades[-self.context_trade_window :],
            self.market.footprint.tick_size,
            value_area_pct=self.value_area_pct,
        )

    def context(self, timestamp: datetime, current_price: float) -> HistoricalPositionContext:
        state = self.market.state()
        profile = self._volume_profile()
        order_flow = self.market.order_flow.snapshot()
        bins = profile.bins if profile else ()
        return HistoricalPositionContext(
            state=state,
            volume_profile=profile,
            order_flow=order_flow,
            current_price=current_price,
            timestamp=timestamp,
            prior_highs=tuple(p for p, _ in bins[-20:]),
            prior_lows=tuple(p for p, _ in bins[:20]),
            swing_highs=(profile.high,) if profile and profile.high > 0 else (),
            swing_lows=(profile.low,) if profile and profile.low > 0 else (),
            trend_strength=state_to_trend_strength(state),
            extra_features=historical_feature_bag(state),
        )

    def evaluate(
        self, trade: Trade, *, timestamp: datetime, current_price: float, hedge_active: bool | None = None
    ) -> PositionAction:
        ctx = self.context(timestamp, current_price)
        trade.record_excursion(current_price)
        return self.position_manager.evaluate(
            trade=trade,
            current_price=current_price,
            order_flow=ctx.order_flow,
            volume_profile=ctx.volume_profile,
            liquidity_events=ctx.state.liquidity_events,
            prior_highs=ctx.prior_highs,
            prior_lows=ctx.prior_lows,
            swing_highs=ctx.swing_highs,
            swing_lows=ctx.swing_lows,
            structure_break_level=ctx.structure_break_level,
            atr=ctx.atr,
            trend_strength=ctx.trend_strength,
            extra_features=ctx.extra_features,
            timestamp=ctx.timestamp,
            hedge_active=hedge_active,
        )

    def on_hedge_opened(self, trade: Trade, timestamp: datetime) -> None:
        self.position_manager.register_hedge(trade.trade_id, timestamp)

    def on_hedge_closed(self, trade: Trade) -> None:
        self.position_manager.clear_hedge(trade.trade_id)


def state_to_trend_strength(state: HistoricalMarketState) -> float:
    scores = [float(state.auction_long_score), float(state.auction_short_score)]
    if state.flow_liquidity_signal is not None:
        scores.append(abs(float(getattr(state.flow_liquidity_signal, "score", 0.0))))
    if not scores:
        return 0.5
    value = max(scores)
    if value > 1.0:
        value /= 10.0
    return max(0.0, min(1.0, value))


def historical_feature_bag(state: HistoricalMarketState) -> dict[str, float]:
    features = {
        "auction_long_score": float(state.auction_long_score),
        "auction_short_score": float(state.auction_short_score),
        "liquidity_event_count": float(len(state.liquidity_events)),
    }
    if state.flow_liquidity_signal is not None:
        score = getattr(state.flow_liquidity_signal, "score", None)
        if score is not None:
            features["flow_liquidity_score"] = float(score)
    return features
