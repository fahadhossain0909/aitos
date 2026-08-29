"""Executed-trade order-flow analytics from real TradeTick data."""

from __future__ import annotations

from collections.abc import Sequence

from aitos.models.market import TradeSide, TradeTick


def _signed_volume(trade: TradeTick) -> float:
    if trade.is_buyer_maker or trade.side == TradeSide.SELL:
        return -abs(trade.quantity)
    return abs(trade.quantity)


def delta(trades: Sequence[TradeTick]) -> float:
    return sum(_signed_volume(t) for t in trades)


def buy_volume_ratio(trades: Sequence[TradeTick]) -> float:
    if not trades:
        return 0.5
    buy = sum(abs(t.quantity) for t in trades if _signed_volume(t) > 0)
    total = sum(abs(t.quantity) for t in trades)
    return buy / total if total else 0.5


def order_flow_bias_score(trades: Sequence[TradeTick]) -> float:
    if not trades:
        return 5.0
    return round(max(0.0, min(10.0, buy_volume_ratio(trades) * 10.0)), 2)


def aggression_ratio(trades: Sequence[TradeTick]) -> float:
    if not trades:
        return 0.0
    total = sum(abs(t.quantity) for t in trades)
    return round(abs(delta(trades)) / total, 4) if total else 0.0


def imbalance_score(trades: Sequence[TradeTick]) -> float:
    return round(aggression_ratio(trades) * 10.0, 2)
