"""Stateful, source-agnostic order-flow engine.

The same engine can consume live WebSocket TradeTick objects or historical
TradeTick batches. It keeps a bounded rolling window and emits normalized
features suitable for the scanner/decision-fusion layer.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from aitos.intelligence.order_flow import (
    aggression_ratio,
    buy_volume_ratio,
    delta,
    imbalance_score,
)
from aitos.models.market import TradeSide, TradeTick


@dataclass(frozen=True)
class OrderFlowFeatures:
    trade_count: int
    buy_volume: float
    sell_volume: float
    delta: float
    cvd: float
    buy_ratio: float
    aggression: float
    imbalance: float
    vwap: float
    last_price: float
    direction: str
    timestamp: datetime | None

    @property
    def bias_score(self) -> float:
        """Backward-compatible 0-10 order-flow bias used by the scanner."""
        return self.imbalance


class OrderFlowEngine:
    """Rolling order-flow state with deterministic batch/live equivalence."""

    def __init__(self, max_trades: int = 5000) -> None:
        if max_trades < 1:
            raise ValueError("max_trades must be >= 1")
        self._max_trades = max_trades
        self._trades: deque[TradeTick] = deque(maxlen=max_trades)
        self._cvd = 0.0

    @property
    def max_trades(self) -> int:
        """Configured rolling trade-window size."""
        return self._max_trades

    def reset(self) -> None:
        self._trades.clear()
        self._cvd = 0.0

    def ingest(self, trade: TradeTick) -> OrderFlowFeatures:
        self._trades.append(trade)
        self._cvd += self._signed(trade)
        return self.features()

    def ingest_many(self, trades: Sequence[TradeTick]) -> OrderFlowFeatures:
        for trade in trades:
            self.ingest(trade)
        return self.features()

    @staticmethod
    def _signed(trade: TradeTick) -> float:
        if trade.is_buyer_maker or trade.side == TradeSide.SELL:
            return -abs(trade.quantity)
        return abs(trade.quantity)

    @property
    def trades(self) -> tuple[TradeTick, ...]:
        return tuple(self._trades)

    def snapshot(self) -> OrderFlowFeatures:
        """Return the current normalized feature snapshot."""
        return self.features()

    def features(self) -> OrderFlowFeatures:
        trades = tuple(self._trades)
        if not trades:
            return OrderFlowFeatures(
                0,
                0.0,
                0.0,
                0.0,
                self._cvd,
                0.5,
                0.0,
                5.0,
                0.0,
                0.0,
                "neutral",
                None,
            )
        buy = sum(abs(t.quantity) for t in trades if self._signed(t) > 0)
        sell = sum(abs(t.quantity) for t in trades if self._signed(t) < 0)
        total = buy + sell
        vwap = (
            sum(t.price * abs(t.quantity) for t in trades) / total
            if total
            else trades[-1].price
        )
        buy_ratio = buy_volume_ratio(trades)
        aggr = aggression_ratio(trades)
        bias = imbalance_score(trades)
        direction = (
            "long" if buy_ratio > 0.55 else "short" if buy_ratio < 0.45 else "neutral"
        )
        return OrderFlowFeatures(
            trade_count=len(trades),
            buy_volume=buy,
            sell_volume=sell,
            delta=delta(trades),
            cvd=self._cvd,
            buy_ratio=round(buy_ratio, 6),
            aggression=aggr,
            imbalance=bias,
            vwap=vwap,
            last_price=trades[-1].price,
            direction=direction,
            timestamp=trades[-1].timestamp,
        )
