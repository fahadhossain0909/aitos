"""Event-driven live market cache backed by canonical MarketData V1."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from aitos.core.contracts import Event
from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.intelligence.trade_depth_matcher import TradeDepthMatcher
from aitos.logging_setup import get_logger
from aitos.market_data.bus import MarketDataBus, market_event_from_wire
from aitos.market_data.contracts import MarketEventType, MarketSource
from aitos.models.market import OrderBookSnapshot, TradeSide, TradeTick

logger = get_logger("aitos.intelligence.live_scanner")
LIVE_SCANNER_GROUP = "live-scanner-cache-v1"
LIVE_TRADE_MAX_AGE_SECONDS = 15.0
LIVE_BOOK_MAX_AGE_SECONDS = 15.0


@dataclass
class LiveSymbolCache:
    trades: deque = field(default_factory=deque)
    last_trade_at: datetime | None = None
    last_book_at: datetime | None = None
    last_trade_source_at: datetime | None = None
    last_book_source_at: datetime | None = None
    last_trade_received_at: datetime | None = None
    last_book_received_at: datetime | None = None
    last_trade_trace_id: str | None = None
    last_book_trace_id: str | None = None
    last_trade_event_id: str | None = None
    last_book_event_id: str | None = None
    order_book: OrderBookSnapshot | None = None
    liquidity_events: deque = field(default_factory=lambda: deque(maxlen=200))
    depth_event_history: deque = field(default_factory=lambda: deque(maxlen=200))
    depth_history_gap_at_ms: int | None = None
    trade_depth_matcher: TradeDepthMatcher = field(default_factory=TradeDepthMatcher)
    stale_trade_rejections: int = 0
    stale_book_rejections: int = 0
    duplicate_trade_rejections: int = 0
    duplicate_book_rejections: int = 0
    hawkes_event_rejections: int = 0
    last_hawkes_timestamp: float | None = None
    last_hawkes_intensities: tuple[float, float] | None = None
    last_stale_trade_source_age_sec: float | None = None
    last_stale_book_source_age_sec: float | None = None


class LiveScannerCache:
    """Maintain scanner state from canonical semantic market-data channels."""

    def __init__(
        self,
        event_bus: EventBus | None,
        symbols: list[str],
        max_trades: int = 5000,
        *,
        hawkes_engine=None,
    ) -> None:
        self._event_bus = event_bus
        self._bus = MarketDataBus(event_bus) if event_bus is not None else None
        self._hawkes_engine = hawkes_engine
        self._symbols = {s.upper() for s in symbols}
        self._max_trades = max(100, max_trades)
        self._state: dict[str, LiveSymbolCache] = {}
        self._subscriptions: list[Subscription] = []
        self._initialized = False

    def _cache(self, symbol: str) -> LiveSymbolCache:
        symbol = symbol.upper()
        if symbol not in self._state:
            self._state[symbol] = LiveSymbolCache(trades=deque(maxlen=self._max_trades))
        return self._state[symbol]

    def snapshot(self, symbol: str) -> LiveSymbolCache | None:
        return self._state.get(symbol.upper())

    def _source_age_seconds(
        self, timestamp: datetime | None, now: datetime | None = None
    ) -> float | None:
        if timestamp is None:
            return None
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - timestamp).total_seconds())

    def is_book_fresh(self, symbol: str, max_age_seconds: float) -> bool:
        state = self._state.get(symbol.upper())
        if state is None or state.order_book is None:
            return False
        age = self._source_age_seconds(state.last_book_source_at or state.last_book_at)
        return age is not None and age <= max_age_seconds

    def is_trade_fresh(self, symbol: str, max_age_seconds: float) -> bool:
        state = self._state.get(symbol.upper())
        if state is None or state.last_trade_at is None:
            return False
        age = self._source_age_seconds(
            state.last_trade_source_at or state.last_trade_at
        )
        return age is not None and age <= max_age_seconds

    async def initialize(self, direct_market_data: bool = False) -> None:
        if self._initialized:
            return
        if self._event_bus is None:
            self._initialized = True
            return
        if direct_market_data:

            async def trade_handler(raw: Event) -> None:
                await self._on_trade_event(market_event_from_wire(raw.payload))

            async def book_handler(raw: Event) -> None:
                await self._on_book_event(market_event_from_wire(raw.payload))

            try:
                self._subscriptions = [
                    await self._event_bus.subscribe(
                        "market.trade",
                        trade_handler,
                        group=LIVE_SCANNER_GROUP,
                        live_only=True,
                    ),
                    await self._event_bus.subscribe(
                        "market.book.snapshot",
                        book_handler,
                        group=LIVE_SCANNER_GROUP,
                        live_only=True,
                    ),
                ]
            except TypeError:
                self._subscriptions = [
                    await self._event_bus.subscribe(
                        "market.trade",
                        trade_handler,
                        group=LIVE_SCANNER_GROUP,
                        start_id="$",
                    ),
                    await self._event_bus.subscribe(
                        "market.book.snapshot",
                        book_handler,
                        group=LIVE_SCANNER_GROUP,
                        start_id="$",
                    ),
                ]
        else:
            assert self._bus is not None
            self._subscriptions = [
                await self._bus.subscribe(
                    MarketEventType.TRADE,
                    self._on_trade_event,
                    group=LIVE_SCANNER_GROUP,
                    live_only=True,
                ),
                await self._bus.subscribe(
                    MarketEventType.BOOK_SNAPSHOT,
                    self._on_book_event,
                    group=LIVE_SCANNER_GROUP,
                    live_only=True,
                ),
            ]
        self._initialized = True

    async def shutdown(self) -> None:
        for sub in self._subscriptions:
            sub.cancel()
        self._subscriptions.clear()
        self._initialized = False

    def _observe_hawkes_trade(self, trade: TradeTick, state: LiveSymbolCache) -> None:
        """Forward accepted aggressor trades to an explicitly registered A-Stat model.

        Exchange trade IDs provide deterministic ordering for equal-millisecond
        timestamps. Equal timestamps are separated by one floating-point ULP;
        genuinely out-of-order timestamps are rejected rather than time-shifted.
        """
        engine = self._hawkes_engine
        if engine is None or not engine.has_hawkes_model(trade.symbol):
            return
        timestamp = trade.timestamp.timestamp()
        previous = state.last_hawkes_timestamp
        if previous is None:
            last_timestamp = getattr(engine, "last_hawkes_timestamp", None)
            if callable(last_timestamp):
                previous = last_timestamp(trade.symbol)
        if previous is not None:
            if timestamp < previous:
                state.hawkes_event_rejections += 1
                return
            if timestamp == previous:
                timestamp = math.nextafter(previous, math.inf)
        mark = 1 if trade.side == TradeSide.BUY else 0
        try:
            snapshot = engine.observe_marked_event(trade.symbol, timestamp, mark)
        except (KeyError, ValueError):
            state.hawkes_event_rejections += 1
            logger.warning(
                "live Hawkes event rejected",
                extra={
                    "aitos_extra": {
                        "symbol": trade.symbol,
                        "trade_id": trade.trade_id,
                        "timestamp": timestamp,
                    }
                },
            )
            return
        state.last_hawkes_timestamp = timestamp
        state.last_hawkes_intensities = snapshot.intensities

    async def accept_live_trade(self, trade: TradeTick) -> None:
        if trade.symbol.upper() not in self._symbols:
            return
        state = self._cache(trade.symbol)
        received_at = datetime.now(timezone.utc)
        if state.trades and trade.trade_id <= state.trades[-1].trade_id:
            state.duplicate_trade_rejections += 1
            try:
                from aitos.forensics.data_flow_hooks import record_drop
                from aitos.forensics.data_flow_schema import (
                    DataIssueClass,
                    PipelineStage,
                )

                record_drop(
                    PipelineStage.LIVE_STATE.value,
                    symbol=str(trade.symbol or ""),
                    issue=DataIssueClass.DUPLICATE.value,
                )
            except Exception:
                pass
            return
        state.trades.append(trade)
        state.trade_depth_matcher.add_trade(trade)
        self._observe_hawkes_trade(trade, state)
        state.last_trade_at = received_at
        state.last_trade_source_at = trade.timestamp
        state.last_trade_received_at = received_at
        try:
            from aitos.forensics.data_flow_hooks import record_input, record_output
            from aitos.forensics.data_flow_schema import PipelineStage

            record_input(PipelineStage.LIVE_STATE.value, symbol=str(trade.symbol or ""))
            record_output(
                PipelineStage.LIVE_STATE.value, symbol=str(trade.symbol or "")
            )
        except Exception:
            pass

    async def accept_live_order_book(self, book: OrderBookSnapshot) -> None:
        if book.symbol.upper() not in self._symbols:
            return
        state = self._cache(book.symbol)
        received_at = datetime.now(timezone.utc)
        if (
            state.order_book is not None
            and state.order_book.last_update_id == book.last_update_id
            and state.order_book.timestamp == book.timestamp
        ):
            state.duplicate_book_rejections += 1
            try:
                from aitos.forensics.data_flow_hooks import record_drop
                from aitos.forensics.data_flow_schema import (
                    DataIssueClass,
                    PipelineStage,
                )

                record_drop(
                    PipelineStage.LIVE_STATE.value,
                    symbol=str(book.symbol or ""),
                    issue=DataIssueClass.DUPLICATE.value,
                )
            except Exception:
                pass
            return
        depth_sequence_gap = bool(
            state.order_book is not None
            and (
                book.depth_event is None
                or int(book.depth_event.get("previous_update_id", -1))
                != state.order_book.last_update_id
            )
        )
        if depth_sequence_gap:
            state.depth_event_history.clear()
            state.trade_depth_matcher.reset()
            state.depth_history_gap_at_ms = int(book.timestamp.timestamp() * 1000)
        state.order_book = book
        if book.depth_event:
            event_payload = dict(book.depth_event)
            if depth_sequence_gap:
                event_payload["sequence_gap"] = True
            depth_event = state.trade_depth_matcher.add_depth_event(event_payload)
            state.depth_event_history.append(depth_event)
        state.last_book_at = received_at
        state.last_book_source_at = book.timestamp
        state.last_book_received_at = received_at
        try:
            from aitos.forensics.data_flow_hooks import record_input, record_output
            from aitos.forensics.data_flow_schema import PipelineStage

            record_input(PipelineStage.LIVE_STATE.value, symbol=str(book.symbol or ""))
            record_output(PipelineStage.LIVE_STATE.value, symbol=str(book.symbol or ""))
        except Exception:
            pass

    async def _on_trade_event(self, event) -> None:
        if event.symbol.upper() not in self._symbols:
            return
        state = self._cache(event.symbol)
        received_at = datetime.now(timezone.utc)
        source_age = (received_at - event.event_time).total_seconds()
        if (
            event.source != MarketSource.WEBSOCKET
            or source_age > LIVE_TRADE_MAX_AGE_SECONDS
        ):
            state.stale_trade_rejections += 1
            state.last_stale_trade_source_age_sec = round(max(0.0, source_age), 3)
            return
        payload = dict(event.payload)
        payload["symbol"] = event.symbol
        payload["timestamp"] = event.event_time.isoformat()
        trade = TradeTick.from_dict(payload)
        if state.trades and trade.trade_id <= state.trades[-1].trade_id:
            state.duplicate_trade_rejections += 1
            return
        state.trades.append(trade)
        state.trade_depth_matcher.add_trade(trade)
        self._observe_hawkes_trade(trade, state)
        state.last_trade_at = received_at
        state.last_trade_source_at = event.event_time
        state.last_trade_received_at = received_at
        state.last_trade_trace_id = getattr(event, "trace_id", None)
        state.last_trade_event_id = getattr(event, "event_id", None)
        try:
            from aitos.forensics.data_flow_hooks import (
                note_trace,
                record_input,
                record_output,
            )
            from aitos.forensics.data_flow_schema import PipelineStage

            record_input(PipelineStage.LIVE_STATE.value, symbol=str(event.symbol or ""))
            record_output(
                PipelineStage.LIVE_STATE.value, symbol=str(event.symbol or "")
            )
            note_trace(
                trace_id=getattr(event, "trace_id", None),
                stage=PipelineStage.LIVE_STATE.value,
                symbol=str(event.symbol or ""),
                event_id=getattr(event, "event_id", None),
                extra={
                    "event_type": event.event_type.value,
                    "phase": "state_updated",
                },
            )
        except Exception:
            pass

    async def _on_book_event(self, event) -> None:
        if event.symbol.upper() not in self._symbols:
            return
        state = self._cache(event.symbol)
        received_at = datetime.now(timezone.utc)
        source_age = (received_at - event.event_time).total_seconds()
        if (
            event.source != MarketSource.WEBSOCKET
            or source_age > LIVE_BOOK_MAX_AGE_SECONDS
        ):
            state.stale_book_rejections += 1
            state.last_stale_book_source_age_sec = round(max(0.0, source_age), 3)
            return
        payload = dict(event.payload)
        payload["symbol"] = event.symbol
        payload["timestamp"] = event.event_time.isoformat()
        book = OrderBookSnapshot.from_dict(payload)
        if (
            state.order_book is not None
            and state.order_book.last_update_id == book.last_update_id
            and state.order_book.timestamp == book.timestamp
        ):
            state.duplicate_book_rejections += 1
            return
        depth_sequence_gap = bool(
            state.order_book is not None
            and (
                book.depth_event is None
                or int(book.depth_event.get("previous_update_id", -1))
                != state.order_book.last_update_id
            )
        )
        if depth_sequence_gap:
            state.depth_event_history.clear()
            state.trade_depth_matcher.reset()
            state.depth_history_gap_at_ms = int(book.timestamp.timestamp() * 1000)
        state.order_book = book
        if book.depth_event:
            event_payload = dict(book.depth_event)
            if depth_sequence_gap:
                event_payload["sequence_gap"] = True
            depth_event = state.trade_depth_matcher.add_depth_event(event_payload)
            state.depth_event_history.append(depth_event)
        state.last_book_at = received_at
        state.last_book_source_at = event.event_time
        state.last_book_received_at = received_at
        state.last_book_trace_id = getattr(event, "trace_id", None)
        state.last_book_event_id = getattr(event, "event_id", None)
        try:
            from aitos.forensics.data_flow_hooks import (
                note_trace,
                record_input,
                record_output,
            )
            from aitos.forensics.data_flow_schema import PipelineStage

            record_input(PipelineStage.LIVE_STATE.value, symbol=str(event.symbol or ""))
            record_output(
                PipelineStage.LIVE_STATE.value, symbol=str(event.symbol or "")
            )
            note_trace(
                trace_id=getattr(event, "trace_id", None),
                stage=PipelineStage.LIVE_STATE.value,
                symbol=str(event.symbol or ""),
                event_id=getattr(event, "event_id", None),
                extra={"event_type": event.event_type.value, "phase": "state_updated"},
            )
        except Exception:
            pass

    async def _on_liquidity(self, event) -> None:
        if event.symbol.upper() in self._symbols:
            self._cache(event.symbol).liquidity_events.append(dict(event.payload))

    def freshness_snapshot(self, symbol: str) -> dict[str, object]:
        state = self._state.get(symbol.upper())
        now = datetime.now(timezone.utc)
        if state is None:
            return {
                "cache_has_state": False,
                "trade_source_age_ms": None,
                "book_source_age_ms": None,
                "trade_receive_lag_ms": None,
                "book_receive_lag_ms": None,
                "trade_consumer_lag_ms": None,
                "book_consumer_lag_ms": None,
                "source_age_ms": None,
                "trade_age_sec": None,
                "book_age_sec": None,
                "trade_consumer_lag_sec": None,
                "book_consumer_lag_sec": None,
            }
        trade_age = (
            (now - (state.last_trade_source_at or state.last_trade_at)).total_seconds()
            * 1000
            if (state.last_trade_source_at or state.last_trade_at)
            else None
        )
        book_age = (
            (now - (state.last_book_source_at or state.last_book_at)).total_seconds()
            * 1000
            if (state.last_book_source_at or state.last_book_at)
            else None
        )
        trade_lag = (
            (state.last_trade_received_at - state.last_trade_source_at).total_seconds()
            * 1000
            if state.last_trade_received_at and state.last_trade_source_at
            else (
                (state.last_trade_received_at - state.last_trade_at).total_seconds()
                * 1000
                if state.last_trade_received_at and state.last_trade_at
                else None
            )
        )
        book_lag = (
            (state.last_book_received_at - state.last_book_source_at).total_seconds()
            * 1000
            if state.last_book_received_at and state.last_book_source_at
            else (
                (state.last_book_received_at - state.last_book_at).total_seconds()
                * 1000
                if state.last_book_received_at and state.last_book_at
                else None
            )
        )
        ages = [v for v in (trade_age, book_age) if v is not None]
        return {
            "cache_has_state": True,
            "trade_source_age_ms": trade_age,
            "book_source_age_ms": book_age,
            "trade_receive_lag_ms": trade_lag,
            "book_receive_lag_ms": book_lag,
            "trade_consumer_lag_ms": trade_lag,
            "book_consumer_lag_ms": book_lag,
            "source_age_ms": max(ages) if ages else None,
            "trade_age_sec": (
                (now - state.last_trade_at).total_seconds()
                if state.last_trade_at
                else None
            ),
            "book_age_sec": (
                (now - state.last_book_at).total_seconds()
                if state.last_book_at
                else None
            ),
            "trade_consumer_lag_sec": (
                trade_lag / 1000 if trade_lag is not None else None
            ),
            "book_consumer_lag_sec": book_lag / 1000 if book_lag is not None else None,
        }
