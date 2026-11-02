"""Binance diff-depth local order-book reconstruction."""

from __future__ import annotations

import heapq
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aitos.logging_setup import get_logger
from aitos.models.market import OrderBookSnapshot

logger = get_logger("aitos.exchange.orderbook")
SLOW_ORDERBOOK_STAGE_SECONDS = 0.05
FORENSIC_SAMPLE_EVERY = 1000


@dataclass(frozen=True)
class DepthUpdate:
    first_update_id: int
    final_update_id: int
    previous_update_id: int
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    event_time_ms: int


class OrderBookSequenceError(RuntimeError):
    """Raised when a diff-depth stream cannot be applied contiguously."""


class LocalOrderBook:
    def __init__(self, symbol: str, max_levels: int = 1000) -> None:
        self.symbol = symbol
        self.max_levels = max(20, max_levels)
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}
        self.last_update_id: int | None = None
        self.initialized = False
        self._awaiting_first_update = False
        self._recent_reductions: dict[tuple[str, float], tuple[int, float]] = {}
        self._forensics: dict[str, Any] = {
            "apply_count": 0,
            "snapshot_count": 0,
            "sequence_errors": 0,
            "stale_updates": 0,
            "last_apply_seconds": 0.0,
            "last_snapshot_seconds": 0.0,
            "last_bid_sort_seconds": 0.0,
            "last_ask_sort_seconds": 0.0,
            "last_bids_size": 0,
            "last_asks_size": 0,
            "max_bids_size": 0,
            "max_asks_size": 0,
            "last_first_update_id": None,
            "last_final_update_id": None,
            "last_previous_update_id": None,
        }

    def forensics_snapshot(self) -> dict[str, Any]:
        """Return lightweight diagnostics for targeted runtime forensics."""
        snapshot = dict(self._forensics)
        snapshot["symbol"] = self.symbol
        snapshot["last_update_id"] = self.last_update_id
        snapshot["initialized"] = self.initialized
        snapshot["awaiting_first_update"] = self._awaiting_first_update
        return snapshot

    def seed_from_diff(self, diff: DepthUpdate) -> None:
        """Initialize the order book from a single depth update (used when REST bootstrap fails)."""
        self._bids = {p: q for p, q in diff.bids if q > 0}
        self._asks = {p: q for p, q in diff.asks if q > 0}
        self.last_update_id = diff.final_update_id
        self.initialized = True
        self._awaiting_first_update = False
        self._forensics["last_bids_size"] = len(self._bids)
        self._forensics["last_asks_size"] = len(self._asks)
        self._forensics["max_bids_size"] = len(self._bids)
        self._forensics["max_asks_size"] = len(self._asks)

    def seed(self, snapshot: OrderBookSnapshot) -> None:
        self._bids = {p: q for p, q in snapshot.bids if q > 0}
        self._asks = {p: q for p, q in snapshot.asks if q > 0}
        self.last_update_id = snapshot.last_update_id
        self.initialized = True
        # REST snapshot and queued websocket diffs overlap; discard covered diffs
        # and require the first newer diff to bridge snapshot.last_update_id.
        self._awaiting_first_update = True
        self._forensics["last_bids_size"] = len(self._bids)
        self._forensics["last_asks_size"] = len(self._asks)
        self._forensics["max_bids_size"] = len(self._bids)
        self._forensics["max_asks_size"] = len(self._asks)

    def apply(self, update: DepthUpdate) -> OrderBookSnapshot | None:
        started = time.perf_counter()
        self._forensics["apply_count"] = int(self._forensics["apply_count"]) + 1
        self._forensics["last_first_update_id"] = update.first_update_id
        self._forensics["last_final_update_id"] = update.final_update_id
        self._forensics["last_previous_update_id"] = update.previous_update_id
        if not self.initialized or self.last_update_id is None:
            self._forensics["sequence_errors"] = (
                int(self._forensics["sequence_errors"]) + 1
            )
            logger.error(
                "order-book forensic sequence error: unseeded book",
                extra={
                    "aitos_extra": {
                        "stage": "orderbook_apply_sequence_error",
                        **self.forensics_snapshot(),
                    }
                },
            )
            raise OrderBookSequenceError(
                "order book must be seeded from REST snapshot first"
            )

        # Skip stale updates silently — they pre-date our local book.
        if update.final_update_id <= self.last_update_id:
            self._forensics["stale_updates"] = int(self._forensics["stale_updates"]) + 1
            self._record_apply_duration(started, update)
            return None

        # The first newer websocket diff must bridge the REST snapshot. A gap
        # invalidates the local book and must trigger a fresh bootstrap upstream.
        if self._awaiting_first_update:
            if not (
                update.first_update_id
                <= self.last_update_id + 1
                <= update.final_update_id
            ):
                self._forensics["sequence_errors"] = (
                    int(self._forensics["sequence_errors"]) + 1
                )
                logger.error(
                    "order-book forensic sequence error: bootstrap bridge mismatch",
                    extra={
                        "aitos_extra": {
                            "stage": "orderbook_apply_sequence_error",
                            "reason": "bootstrap_bridge_mismatch",
                            **self.forensics_snapshot(),
                        }
                    },
                )
                raise OrderBookSequenceError(
                    f"first diff does not bridge snapshot for {self.symbol}: "
                    f"snapshot={self.last_update_id}, U={update.first_update_id}, "
                    f"u={update.final_update_id}"
                )
            self._awaiting_first_update = False
        elif update.previous_update_id != self.last_update_id:
            self._forensics["sequence_errors"] = (
                int(self._forensics["sequence_errors"]) + 1
            )
            logger.error(
                "order-book forensic sequence error: chain break",
                extra={
                    "aitos_extra": {
                        "stage": "orderbook_apply_sequence_error",
                        "reason": "chain_break",
                        **self.forensics_snapshot(),
                    }
                },
            )
            raise OrderBookSequenceError(
                f"depth chain break for {self.symbol}: pu={update.previous_update_id}, local={self.last_update_id}"
            )

        depth_event = self._calculate_depth_event(update)
        self._apply_levels(self._bids, update.bids)
        self._apply_levels(self._asks, update.asks)
        self.last_update_id = update.final_update_id
        result = self.snapshot(update.event_time_ms, depth_event=depth_event)
        self._record_apply_duration(started, update)
        return result

    def _record_apply_duration(self, started: float, update: DepthUpdate) -> None:
        elapsed = time.perf_counter() - started
        self._forensics["last_apply_seconds"] = elapsed
        self._forensics["last_bids_size"] = len(self._bids)
        self._forensics["last_asks_size"] = len(self._asks)
        self._forensics["max_bids_size"] = max(
            int(self._forensics["max_bids_size"]), len(self._bids)
        )
        self._forensics["max_asks_size"] = max(
            int(self._forensics["max_asks_size"]), len(self._asks)
        )
        if (
            elapsed >= SLOW_ORDERBOOK_STAGE_SECONDS
            or int(self._forensics["apply_count"]) % FORENSIC_SAMPLE_EVERY == 0
        ):
            logger.info(
                "order-book forensic apply sample",
                extra={
                    "aitos_extra": {
                        "stage": "orderbook_apply",
                        "duration_seconds": elapsed,
                        "update_first": update.first_update_id,
                        "update_final": update.final_update_id,
                        "update_previous": update.previous_update_id,
                        **self.forensics_snapshot(),
                    }
                },
            )

    def _calculate_depth_event(self, update: DepthUpdate) -> dict[str, float]:
        """Measure full-depth liquidity changes for this sequenced diff event.

        Reductions are cancellation proxies: Binance L2 does not reveal whether
        resting quantity was cancelled or executed. Do not label them as facts.
        """
        best_bid = max(self._bids, default=0.0)
        best_ask = min(self._asks, default=0.0)
        mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else 0.0
        weighted_ofi = 0.0
        bid_add = bid_reduce = ask_add = ask_reduce = replenished = 0.0
        reductions: list[dict[str, Any]] = []
        now_ms = update.event_time_ms or int(time.time() * 1000)
        horizon_ms = 5000
        for key, (ts, _) in tuple(self._recent_reductions.items()):
            if now_ms - ts > horizon_ms:
                self._recent_reductions.pop(key, None)
        for side, levels, book, sign in (
            ("bid", update.bids, self._bids, 1.0),
            ("ask", update.asks, self._asks, -1.0),
        ):
            for price, new_qty in levels:
                old_qty = book.get(price, 0.0)
                change = float(new_qty) - old_qty
                if abs(change) <= 1e-12:
                    continue
                distance_bps = abs(price - mid) / mid * 10000.0 if mid > 0 else 0.0
                weight = max(0.05, min(1.0, 2.718281828 ** (-distance_bps / 20.0)))
                weighted_ofi += sign * change * weight
                key = (side, price)
                if change > 0:
                    if side == "bid":
                        bid_add += change
                    else:
                        ask_add += change
                    prior = self._recent_reductions.get(key)
                    if prior is not None and now_ms - prior[0] <= horizon_ms:
                        restored = min(change, prior[1])
                        replenished += restored
                        remaining = prior[1] - restored
                        if remaining > 1e-12:
                            self._recent_reductions[key] = (prior[0], remaining)
                        else:
                            self._recent_reductions.pop(key, None)
                else:
                    reduced = -change
                    reductions.append(
                        {
                            "side": side,
                            "price": float(price),
                            "quantity": float(reduced),
                        }
                    )
                    if side == "bid":
                        bid_reduce += reduced
                    else:
                        ask_reduce += reduced
                    prior = self._recent_reductions.get(key)
                    self._recent_reductions[key] = (
                        now_ms,
                        reduced
                        + (
                            prior[1]
                            if prior and now_ms - prior[0] <= horizon_ms
                            else 0.0
                        ),
                    )
        scale = sum(sorted(self._bids.values(), reverse=True)[:20])
        scale += sum(sorted(self._asks.values())[:20])
        normalized = weighted_ofi / max(scale, 1e-12)
        reductions = bid_reduce + ask_reduce
        return {
            "ofi_raw": weighted_ofi,
            "ofi_normalized": normalized,
            "ofi_scale": scale,
            "ofi_score": max(0.0, min(10.0, 5.0 + 50.0 * normalized)),
            "bid_add_qty": bid_add,
            "bid_reduce_qty": bid_reduce,
            "ask_add_qty": ask_add,
            "ask_reduce_qty": ask_reduce,
            "inferred_cancel_qty": reductions,
            "replenishment_qty": replenished,
            "addition_qty": bid_add + ask_add,
            "reduction_qty": reductions,
            "replenishment_ratio": replenished / max(reductions, 1e-12),
            "event_time_ms": float(now_ms),
            "first_update_id": float(update.first_update_id),
            "final_update_id": float(update.final_update_id),
            "previous_update_id": float(update.previous_update_id),
            "changed_level_count": float(len(update.bids) + len(update.asks)),
            "reductions": reductions,
            "matched_execution_qty": 0.0,
            "unmatched_reduction_qty": reductions_total := (bid_reduce + ask_reduce),
            "attribution_pending_qty": reductions_total,
            "cancellation_proxy_qty": 0.0,
            "execution_attributed_ratio": 0.0,
            "attribution_complete": not reductions,
        }

    @staticmethod
    def _apply_levels(
        book: dict[float, float], levels: Iterable[tuple[float, float]]
    ) -> None:
        for price, quantity in levels:
            if quantity <= 0:
                book.pop(price, None)
            else:
                book[price] = quantity

    def snapshot(
        self, event_time_ms: int = 0, *, depth_event: dict[str, float] | None = None
    ) -> OrderBookSnapshot:
        started = time.perf_counter()
        sort_started = time.perf_counter()
        # Keep the complete book for correctness, but only select the requested
        # top-N levels. Sorting the entire dictionary on every 100ms update was
        # an avoidable O(N log N) event-loop hotspot when the book grew large.
        bids = tuple(
            heapq.nlargest(self.max_levels, self._bids.items(), key=lambda x: x[0])
        )
        bid_sort = time.perf_counter() - sort_started
        sort_started = time.perf_counter()
        asks = tuple(
            heapq.nsmallest(self.max_levels, self._asks.items(), key=lambda x: x[0])
        )
        ask_sort = time.perf_counter() - sort_started
        elapsed = time.perf_counter() - started
        self._forensics["snapshot_count"] = int(self._forensics["snapshot_count"]) + 1
        self._forensics["last_snapshot_seconds"] = elapsed
        self._forensics["last_bid_sort_seconds"] = bid_sort
        self._forensics["last_ask_sort_seconds"] = ask_sort
        self._forensics["last_bids_size"] = len(self._bids)
        self._forensics["last_asks_size"] = len(self._asks)
        self._forensics["max_bids_size"] = max(
            int(self._forensics["max_bids_size"]), len(self._bids)
        )
        self._forensics["max_asks_size"] = max(
            int(self._forensics["max_asks_size"]), len(self._asks)
        )
        if (
            elapsed >= SLOW_ORDERBOOK_STAGE_SECONDS
            or bid_sort >= SLOW_ORDERBOOK_STAGE_SECONDS
            or ask_sort >= SLOW_ORDERBOOK_STAGE_SECONDS
            or int(self._forensics["snapshot_count"]) % FORENSIC_SAMPLE_EVERY == 0
        ):
            logger.info(
                "order-book forensic snapshot sample",
                extra={
                    "aitos_extra": {
                        "stage": "orderbook_snapshot",
                        "duration_seconds": elapsed,
                        "bid_sort_seconds": bid_sort,
                        "ask_sort_seconds": ask_sort,
                        **self.forensics_snapshot(),
                    }
                },
            )
        timestamp = (
            datetime.fromtimestamp(event_time_ms / 1000, tz=timezone.utc)
            if event_time_ms
            else datetime.now(timezone.utc)
        )
        return OrderBookSnapshot(
            symbol=self.symbol,
            bids=bids,
            asks=asks,
            last_update_id=self.last_update_id or 0,
            timestamp=timestamp,
            depth_event=depth_event,
        )

    def reset(self) -> None:
        self._bids.clear()
        self._asks.clear()
        self.last_update_id = None
        self.initialized = False
        self._awaiting_first_update = False
        self._recent_reductions.clear()
