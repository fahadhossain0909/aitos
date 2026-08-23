"""Streaming L2 liquidity state tracking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from aitos.models.market import OrderBookSnapshot, TradeSide, TradeTick


@dataclass(frozen=True)
class LiquidityEvent:
    kind: str
    side: str
    score: float
    price: float
    details: str


@dataclass
class _BookState:
    snapshot: OrderBookSnapshot


class LiquidityTracker:
    def __init__(self, min_level_qty: float = 0.0, removal_ratio: float = 0.35) -> None:
        self._previous: Optional[_BookState] = None
        self.min_level_qty = max(0.0, min_level_qty)
        self.removal_ratio = min(0.95, max(0.05, removal_ratio))

    @staticmethod
    def _side_map(levels: Sequence) -> dict[float, float]:
        """Normalize tuple/list and mapping-style book levels to numeric values."""
        normalized: dict[float, float] = {}
        if isinstance(levels, Mapping):
            iterator = levels.items()
        else:
            iterator = levels
        for level in iterator:
            if isinstance(level, Mapping):
                price = level.get("price")
                qty = level.get("quantity", level.get("qty", level.get("size")))
            else:
                try:
                    price, qty = level
                except (TypeError, ValueError):
                    continue
            try:
                normalized[float(price)] = max(0.0, float(qty))
            except (TypeError, ValueError):
                continue
        return normalized

    def update(
        self, snapshot: OrderBookSnapshot, trades: Sequence[TradeTick] = ()
    ) -> list[LiquidityEvent]:
        events: list[LiquidityEvent] = []
        previous = self._previous.snapshot if self._previous else None
        if previous is not None:
            events.extend(self._book_changes(previous, snapshot))
            events.extend(self._detect_sweep(previous, snapshot, trades))
        self._previous = _BookState(snapshot)
        return events

    def _book_changes(
        self, previous: OrderBookSnapshot, current: OrderBookSnapshot
    ) -> list[LiquidityEvent]:
        events: list[LiquidityEvent] = []
        for side_name, old_levels, new_levels in (
            ("bid", previous.bids, current.bids),
            ("ask", previous.asks, current.asks),
        ):
            old = self._side_map(old_levels)
            new = self._side_map(new_levels)
            for price, old_qty in old.items():
                if old_qty <= self.min_level_qty:
                    continue
                new_qty = new.get(price, 0.0)
                change = new_qty - old_qty
                if abs(change) / old_qty < self.removal_ratio:
                    continue
                kind = "stacking" if change > 0 else "pulling"
                score = min(10.0, abs(change) / old_qty * 10.0)
                events.append(
                    LiquidityEvent(
                        kind,
                        side_name,
                        round(score, 2),
                        price,
                        f"qty {old_qty:.6g}->{new_qty:.6g}",
                    )
                )
        return events

    def _detect_sweep(
        self,
        previous: OrderBookSnapshot,
        current: OrderBookSnapshot,
        trades: Sequence[TradeTick],
    ) -> list[LiquidityEvent]:
        if not trades:
            return []
        events: list[LiquidityEvent] = []
        # Always normalize book levels first: OrderBookSnapshot may expose either
        # tuple/list levels or mapping-style levels depending on the adapter.
        prev_bids = self._side_map(previous.bids)
        prev_asks = self._side_map(previous.asks)
        curr_bids = self._side_map(current.bids)
        curr_asks = self._side_map(current.asks)
        prev_bid_qty = sum(prev_bids.values())
        prev_ask_qty = sum(prev_asks.values())
        curr_bid_qty = sum(curr_bids.values())
        curr_ask_qty = sum(curr_asks.values())
        buy_qty = sum(
            t.quantity
            for t in trades
            if not t.is_buyer_maker and t.side == TradeSide.BUY
        )
        sell_qty = sum(
            t.quantity for t in trades if t.is_buyer_maker or t.side == TradeSide.SELL
        )
        if (
            prev_ask_qty > 0
            and curr_ask_qty / prev_ask_qty < 1.0 - self.removal_ratio
            and buy_qty > 0
        ):
            score = min(
                10.0,
                (1.0 - curr_ask_qty / prev_ask_qty) * 10.0
                + min(5.0, buy_qty / max(prev_ask_qty, 1e-12) * 5.0),
            )
            events.append(
                LiquidityEvent(
                    "sweep",
                    "ask",
                    round(score, 2),
                    current.best_ask,
                    "ask liquidity removed with aggressive buying",
                )
            )
        if (
            prev_bid_qty > 0
            and curr_bid_qty / prev_bid_qty < 1.0 - self.removal_ratio
            and sell_qty > 0
        ):
            score = min(
                10.0,
                (1.0 - curr_bid_qty / prev_bid_qty) * 10.0
                + min(5.0, sell_qty / max(prev_bid_qty, 1e-12) * 5.0),
            )
            events.append(
                LiquidityEvent(
                    "sweep",
                    "bid",
                    round(score, 2),
                    current.best_bid,
                    "bid liquidity removed with aggressive selling",
                )
            )
        return events

    def pressure_score(self, snapshot: OrderBookSnapshot) -> float:
        """0-10; 5 neutral, >5 bid pressure, <5 ask pressure."""
        ratio = snapshot.depth_ratio
        if ratio <= 0 or ratio == float("inf"):
            return 5.0
        import math

        return round(max(0.0, min(10.0, 5.0 + math.log(ratio) * 2.5)), 2)
