"""Bridge historical market state into the canonical PositionManager.

This module deliberately contains no alternate trading intelligence. It turns
historical adapter output into the exact feature objects PositionManager
expects and keeps the primary/hedge lifecycle explicit for replay runners.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from aitos.backtest.market_adapter import HistoricalMarketAdapter, HistoricalMarketState
from aitos.intelligence.amt.volume_profile import VolumeProfile, build_volume_profile
from aitos.intelligence.order_flow_engine import OrderFlowFeatures
from aitos.models.trade import Trade, TradeSide
from aitos.trading.position_manager import PositionAction, PositionManager


@dataclass(frozen=True)
class HistoricalPositionContext:
    """Inputs reconstructed from the same historical stream used by the adapter."""

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
    """Canonical PositionManager adapter for historical replay.

    Entry selection remains an explicit callback supplied by the backtest
    strategy. Once a position exists, all HOLD/MANAGE/EXIT and hedge decisions
    come from PositionManager; this class never reimplements those decisions.
    """

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
        self._trade_history: list[Any] = []

    def _volume_profile(self) -> VolumeProfile | None:
        trades = getattr(self.market, "_trades", ())
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
        order_flow: OrderFlowFeatures | None = None
        # OrderFlowEngine exposes its latest feature snapshot through the
        # public compute path in supported versions. Fail closed when absent.
        compute = getattr(self.market.order_flow, "features", None)
        if callable(compute):
            candidate = compute()
            if isinstance(candidate, OrderFlowFeatures):
                order_flow = candidate
        return HistoricalPositionContext(
            state=state,
            volume_profile=profile,
            order_flow=order_flow,
            current_price=current_price,
            timestamp=timestamp,
            prior_highs=tuple(p for p, _ in profile.bins[-20:]) if profile else (),
            prior_lows=tuple(p for p, _ in profile.bins[:20]) if profile else (),
            swing_highs=(profile.high,) if profile and profile.high > 0 else (),
            swing_lows=(profile.low,) if profile and profile.low > 0 else (),
            trend_strength=state_to_trend_strength(state),
            extra_features=_historical_feature_bag(state),
        )

    def evaluate(self, trade: Trade, *, timestamp: datetime, current_price: float) -> PositionAction:
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
        )

    def on_hedge_opened(self, trade: Trade, timestamp: datetime) -> None:
        self.position_manager.register_hedge(trade.trade_id, timestamp)

    def on_hedge_closed(self, trade: Trade) -> None:
        self.position_manager.clear_hedge(trade.trade_id)


def state_to_trend_strength(state: HistoricalMarketState) -> float:
    """Derive a bounded deterministic trend-strength value from available state."""
    scores = [state.auction_long_score, state.auction_short_score]
    if state.flow_liquidity_signal is not None:
        scores.append(abs(float(getattr(state.flow_liquidity_signal, "score", 0.0))))
    if not scores:
        return 0.5
    value = max(scores)
    if value > 1.0:
        value /= 10.0
    return max(0.0, min(1.0, value))


def _historical_feature_bag(state: HistoricalMarketState) -> dict[str, float]:
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


EntrySignal = Callable[[HistoricalPositionContext], tuple[TradeSide, float, float] | None]


def make_decision_callback(
    adapter: HistoricalPositionManagerAdapter,
    trade: Trade | None,
    entry_signal: EntrySignal | None = None,
) -> Callable[[Any], Any]:
    """Create a replay callback while keeping entry policy separate from management.

    ``entry_signal`` returns (side, quantity, stop_distance) for a new position.
    Existing positions are always evaluated through PositionManager.
    """
    active = {"trade": trade}

    def decide(state: Any) -> Any:
        from aitos.backtest.aitos_runner import HistoricalDecision

        latest = state.latest_trade
        if latest is None:
            return HistoricalDecision("flat", 0.0, 0.0)
        context = adapter.context(latest.timestamp, latest.price)
        current = active["trade"]
        if current is None and entry_signal is not None:
            signal = entry_signal(context)
            if signal is not None:
                side, quantity, stop_distance = signal
                entry = latest.price
                sl = entry - stop_distance if side == TradeSide.LONG else entry + stop_distance
                active["trade"] = Trade(
                    trade_id=f"hist-{latest.timestamp.timestamp_ns()}",
                    symbol=latest.symbol,
                    side=side,
                    entry_price=entry,
                    quantity=quantity,
                    leverage=1.0,
                    position_size_usd=entry * quantity,
                    risk_amount_usd=abs(stop_distance * quantity),
                    strategy_id="historical-canonical",
                    agent_consensus={},
                    explanation="historical entry callback",
                    sl_price=sl,
                    tp_price=entry + 2 * stop_distance if side == TradeSide.LONG else entry - 2 * stop_distance,
                    state="position_opened",
                    entry_time=latest.timestamp.isoformat(),
                )
                return HistoricalDecision(side.value.lower(), 1.0, quantity)
        if current is None:
            return HistoricalDecision("flat", 0.0, 0.0)
        action = adapter.evaluate(current, timestamp=latest.timestamp, current_price=latest.price)
        if action.action.value == "exit":
            return HistoricalDecision("flat", 0.0, 0.0)
        if action.action.value == "manage" and action.new_stop_price is not None:
            return HistoricalDecision(current.side.value.lower(), 1.0, 0.0)
        if action.hedge_decision and action.hedge_decision.action.value == "open":
            return HistoricalDecision(
                TradeSide.SHORT.value.lower() if current.side == TradeSide.LONG else TradeSide.LONG.value.lower(),
                action.hedge_decision.confidence,
                current.quantity * action.hedge_decision.size_fraction,
            )
        return HistoricalDecision("flat", 0.0, 0.0)

    return decide
