"""Match L2 depth reductions to aggressive executions without overstating cancels.

Binance Futures diff-depth is an aggregated L2 feed. A reduction at a price can
be caused by executions, cancellations, or both. This matcher attributes only
same-price, same-aggressor-side trade quantity inside a bounded exchange-time
window; the remainder stays provisional until the window closes.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from aitos.models.market import TradeSide, TradeTick

MATCH_WINDOW_MS = 1_000
PRICE_EPSILON = 1e-8
MAX_PENDING_ITEMS = 2_000


class TradeDepthMatcher:
    """Bounded, per-symbol matcher for depth reductions and public trades."""

    def __init__(
        self,
        *,
        match_window_ms: int = MATCH_WINDOW_MS,
        max_pending_items: int = MAX_PENDING_ITEMS,
    ) -> None:
        self.match_window_ms = max(1, int(match_window_ms))
        self.max_pending_items = max(100, int(max_pending_items))
        self._trades: deque[dict[str, Any]] = deque()
        self._reductions: deque[dict[str, Any]] = deque()
        self._watermark_ms = 0

    @staticmethod
    def _timestamp_ms(value: Any) -> int:
        if hasattr(value, "timestamp"):
            return int(value.timestamp() * 1000)
        return int(value)

    def add_trade(self, trade: TradeTick) -> None:
        """Add one deduplicated public trade and reconcile pending reductions."""
        if trade.quantity <= 0 or trade.price <= 0 or trade.timestamp is None:
            return
        side = "bid" if trade.side == TradeSide.SELL else "ask"
        timestamp_ms = self._timestamp_ms(trade.timestamp)
        self._watermark_ms = max(self._watermark_ms, timestamp_ms)
        self._trades.append(
            {
                "side": side,
                "price": float(trade.price),
                "remaining_qty": float(trade.quantity),
                "timestamp_ms": timestamp_ms,
                "trade_id": int(trade.trade_id),
            }
        )
        self._reconcile()

    def add_depth_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Track per-price reductions and reconcile trades already received."""
        if not event:
            return event
        timestamp_ms = int(event.get("event_time_ms", 0))
        if timestamp_ms <= 0:
            return event
        self._watermark_ms = max(self._watermark_ms, timestamp_ms)
        for reduction in event.get("reductions", ()):
            quantity = max(0.0, float(reduction.get("quantity", 0.0)))
            price = float(reduction.get("price", 0.0))
            side = str(reduction.get("side", ""))
            if quantity <= 0 or price <= 0 or side not in {"bid", "ask"}:
                continue
            reduction.update(
                {
                    "matched_execution_qty": 0.0,
                    "unmatched_reduction_qty": quantity,
                    "attribution_pending_qty": quantity,
                    "cancellation_proxy_qty": 0.0,
                    "attribution_complete": False,
                }
            )
            self._reductions.append(
                {
                    "side": side,
                    "price": price,
                    "remaining_qty": quantity,
                    "timestamp_ms": timestamp_ms,
                    "event": event,
                    "level": reduction,
                }
            )
        self._reconcile()
        return event

    def _reconcile(self) -> None:
        cutoff = self._watermark_ms - self.match_window_ms
        # Expired trades cannot explain future book reductions.
        self._trades = deque(
            item
            for item in self._trades
            if item["timestamp_ms"] >= cutoff and item["remaining_qty"] > 1e-12
        )
        # Keep reduction records until their event has been finalized. The event
        # object itself is also retained by the scanner's bounded history.
        self._reductions = deque(
            item
            for item in self._reductions
            if item["timestamp_ms"] + self.match_window_ms >= self._watermark_ms
            and item["remaining_qty"] > 1e-12
        )

        for reduction in self._reductions:
            if reduction["remaining_qty"] <= 1e-12:
                continue
            for trade in self._trades:
                if trade["remaining_qty"] <= 1e-12:
                    continue
                if reduction["side"] != trade["side"]:
                    continue
                if abs(reduction["timestamp_ms"] - trade["timestamp_ms"]) > self.match_window_ms:
                    continue
                tolerance = max(abs(reduction["price"]), abs(trade["price"]), 1.0) * PRICE_EPSILON
                if abs(reduction["price"] - trade["price"]) > tolerance:
                    continue
                matched = min(reduction["remaining_qty"], trade["remaining_qty"])
                reduction["remaining_qty"] -= matched
                trade["remaining_qty"] -= matched
                level = reduction["level"]
                level["matched_execution_qty"] = round(
                    level["matched_execution_qty"] + matched, 12
                )
                level["unmatched_reduction_qty"] = round(
                    max(0.0, level["quantity"] - level["matched_execution_qty"]), 12
                )
                level["attribution_pending_qty"] = level["unmatched_reduction_qty"]

        # Finalize only after the event-time watermark passes the full matching
        # window. Before that, unmatched quantity is explicitly provisional.
        events: dict[int, dict[str, Any]] = {}
        for item in self._reductions:
            events[id(item["event"])] = item["event"]
        for event in events.values():
            levels = event.get("reductions", ())
            for level in levels:
                pending = float(level.get("unmatched_reduction_qty", 0.0))
                complete = (
                    self._watermark_ms
                    >= int(event.get("event_time_ms", 0)) + self.match_window_ms
                )
                level["attribution_pending_qty"] = 0.0 if complete else pending
                level["cancellation_proxy_qty"] = pending if complete else 0.0
                level["attribution_complete"] = complete
            event["matched_execution_qty"] = round(
                sum(float(x.get("matched_execution_qty", 0.0)) for x in levels), 12
            )
            event["unmatched_reduction_qty"] = round(
                sum(float(x.get("unmatched_reduction_qty", 0.0)) for x in levels), 12
            )
            event["attribution_pending_qty"] = round(
                sum(float(x.get("attribution_pending_qty", 0.0)) for x in levels), 12
            )
            event["cancellation_proxy_qty"] = round(
                sum(float(x.get("cancellation_proxy_qty", 0.0)) for x in levels), 12
            )
            total = float(event.get("reduction_qty", 0.0))
            event["execution_attributed_ratio"] = round(
                event["matched_execution_qty"] / max(total, 1e-12), 4
            )
            event["attribution_complete"] = all(
                bool(x.get("attribution_complete", False)) for x in levels
            ) if levels else True

        while len(self._trades) > self.max_pending_items:
            self._trades.popleft()
        while len(self._reductions) > self.max_pending_items:
            self._reductions.popleft()

    def reset(self) -> None:
        self._trades.clear()
        self._reductions.clear()
        self._watermark_ms = 0
