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

    def seed(self, snapshot: OrderBookSnapshot) -> None:
        self._bids = {p: q for p, q in snapshot.bids if q > 0}
        self._asks = {p: q for p, q in snapshot.asks if q > 0}
        self.last_update_id = snapshot.last_update_id
        self.initialized = True
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

        # The websocket producer starts before the REST snapshot is fetched.
        # Therefore the queue can contain updates that the REST snapshot already
        # covers. While awaiting the first bridge update, discard those stale
        # updates without emitting a synthetic snapshot.
        if (
            self._awaiting_first_update
            and update.final_update_id <= self.last_update_id
        ):
            self._forensics["stale_updates"] = int(self._forensics["stale_updates"]) + 1
            self._record_apply_duration(started, update)
            return None

        if update.final_update_id <= self.last_update_id:
            self._forensics["stale_updates"] = int(self._forensics["stale_updates"]) + 1
            result = self.snapshot(update.event_time_ms)
            self._record_apply_duration(started, update)
            return result
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
                    f"first diff does not bridge snapshot for {self.symbol}: snapshot={self.last_update_id}, U={update.first_update_id}, u={update.final_update_id}"
                )
            self._awaiting_first_update = False
        else:
            if update.previous_update_id != self.last_update_id:
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
        self._apply_levels(self._bids, update.bids)
        self._apply_levels(self._asks, update.asks)
        self.last_update_id = update.final_update_id
        result = self.snapshot(update.event_time_ms)
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

    @staticmethod
    def _apply_levels(
        book: dict[float, float], levels: Iterable[tuple[float, float]]
    ) -> None:
        for price, quantity in levels:
            if quantity <= 0:
                book.pop(price, None)
            else:
                book[price] = quantity

    def snapshot(self, event_time_ms: int = 0) -> OrderBookSnapshot:
        started = time.perf_counter()
        sort_started = time.perf_counter()
        # Keep the complete book for correctness, but only select the requested
        # top-N levels. Sorting the entire dictionary on every 100ms update was
        # an avoidable O(N log N) event-loop hotspot when the book grew large.
        bids = tuple(heapq.nlargest(self.max_levels, self._bids.items(), key=lambda x: x[0]))
        bid_sort = time.perf_counter() - sort_started
        sort_started = time.perf_counter()
        asks = tuple(heapq.nsmallest(self.max_levels, self._asks.items(), key=lambda x: x[0]))
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
        )

    def reset(self) -> None:
        self._bids.clear()
        self._asks.clear()
        self.last_update_id = None
        self.initialized = False
        self._awaiting_first_update = False
