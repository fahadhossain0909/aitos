"""Lossless live market-data ingestion for AITOS."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventPriority,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.core.exceptions import ModuleNotInitializedError
from aitos.data.repository import MarketDataRepository
from aitos.eventbus.redis_bus import EventBus
from aitos.exchange.base import ExchangeAdapter
from aitos.intelligence.live_state import LiveMarketStateStore
from aitos.logging_setup import get_logger
from aitos.models.market import Kline, OrderBookSnapshot, TradeTick

logger = get_logger("aitos.data.ingestion")

TRADE_STREAM_IDLE_TIMEOUT_SECONDS = 30.0
TRADE_STREAM_RESTART_DELAY_SECONDS = 1.0
TRADE_STREAM_QUEUE_SIZE = 10_000
TRADE_STREAM_BATCH_SIZE = 64
TRADE_STREAM_BATCH_WAIT_SECONDS = 0.010
TRADE_SINK_CONCURRENCY = 8
TRADE_FALLBACK_LIMIT = 500
ORDERBOOK_PERSIST_INTERVAL_SECONDS = 1.0
STREAM_RESTART_DELAY_SECONDS = 1.0


def kline_topic(symbol: str, timeframe: str) -> str:
    return f"market.kline.{symbol}.{timeframe}"


def trade_topic(symbol: str) -> str:
    return f"market.trade.{symbol}"


def orderbook_topic(symbol: str) -> str:
    return f"market.orderbook.{symbol}"


def liquidity_topic(symbol: str) -> str:
    return f"market.liquidity.{symbol}"


def orderflow_topic(symbol: str) -> str:
    return f"market.orderflow.{symbol}"


def live_state_topic(symbol: str) -> str:
    return f"market.live_state.{symbol}"


class DataIngestionService(AITOSModule):
    """Live market ingestion with bounded lossless backpressure."""

    def __init__(
        self,
        exchange: ExchangeAdapter,
        event_bus: EventBus,
        symbols: List[str],
        kline_timeframe: str = "1m",
        repository: Optional[MarketDataRepository] = None,
        orderbook_levels: int = 20,
        liquidity_trade_window: int = 500,
    ) -> None:
        self._exchange = exchange
        self._event_bus = event_bus
        self._repository = repository
        self._symbols = symbols
        self._kline_timeframe = kline_timeframe
        self._orderbook_levels = orderbook_levels
        self._liquidity_trade_window = max(50, liquidity_trade_window)
        self._initialized = False
        self._tasks: List[asyncio.Task] = []
        self._last_event_time: Optional[str] = None
        self._ticks_processed = 0
        self._liquidity_events = 0
        self._orderflow_events = 0
        self._errors = 0
        self._trade_events_received = 0
        self._trade_parse_errors = 0
        self._trade_stream_errors = 0
        self._trade_downstream_errors = 0
        self._trade_stream_restarts = 0
        self._trade_stream_idle_timeouts = 0
        self._trade_stream_messages_received = 0
        self._trade_stream_queue_waits = 0
        self._trade_stream_max_queue_depth = 0
        self._trade_stream_dropped = 0
        self._last_trade_event_time: Optional[str] = None
        self._last_book_persist_at: Dict[str, datetime] = {}
        self._last_trade_ids: Dict[str, int] = {}
        self._trade_sink_semaphore = asyncio.Semaphore(TRADE_SINK_CONCURRENCY)
        self._live_state = LiveMarketStateStore(
            max_trades=max(5000, self._liquidity_trade_window)
        )
        self._recent_trades = self._live_state.trades

    @property
    def module_id(self) -> str:
        return "data-ingestion-service"

    @property
    def version(self) -> str:
        return "1.7.1"

    async def initialize(self, config: Dict[str, Any]) -> None:
        if self._initialized:
            return
        await self._exchange.connect()
        self._tasks = [
            asyncio.create_task(self._run_kline_stream(), name="aitos-kline-stream"),
            asyncio.create_task(self._run_trade_stream(), name="aitos-trade-stream"),
            asyncio.create_task(
                self._run_orderbook_stream(), name="aitos-orderbook-stream"
            ),
        ]
        self._initialized = True
        logger.info(
            "data ingestion stream tasks started",
            extra={
                "aitos_extra": {
                    "tasks": [t.get_name() for t in self._tasks],
                    "queue_size": TRADE_STREAM_QUEUE_SIZE,
                    "batch_size": TRADE_STREAM_BATCH_SIZE,
                    "sink_concurrency": TRADE_SINK_CONCURRENCY,
                }
            },
        )

    async def health_check(self) -> HealthStatus:
        states = []
        for task in self._tasks:
            state = {
                "name": task.get_name(),
                "done": task.done(),
                "cancelled": task.cancelled(),
            }
            if task.done() and not task.cancelled():
                try:
                    exc = task.exception()
                except Exception as error:
                    exc = error
                if exc is not None:
                    state.update(
                        {"exception_type": type(exc).__name__, "exception": str(exc)}
                    )
            states.append(state)
        alive = sum(not t.done() for t in self._tasks)
        status = (
            ModuleStatus.UNHEALTHY
            if alive < len(self._tasks)
            else (ModuleStatus.DEGRADED if self._errors else ModuleStatus.HEALTHY)
        )
        return HealthStatus(
            module_id=self.module_id,
            status=status,
            latency_ms=0.0,
            last_event_time=self._last_event_time,
            details={
                "ticks_processed": self._ticks_processed,
                "liquidity_events": self._liquidity_events,
                "orderflow_events": self._orderflow_events,
                "errors": self._errors,
                "tasks_alive": alive,
                "trade_events_received": self._trade_events_received,
                "trade_parse_errors": self._trade_parse_errors,
                "trade_stream_errors": self._trade_stream_errors,
                "trade_downstream_errors": self._trade_downstream_errors,
                "trade_stream_restarts": self._trade_stream_restarts,
                "trade_stream_idle_timeouts": self._trade_stream_idle_timeouts,
                "trade_stream_messages_received": self._trade_stream_messages_received,
                "trade_stream_queue_waits": self._trade_stream_queue_waits,
                "trade_stream_max_queue_depth": self._trade_stream_max_queue_depth,
                "trade_stream_dropped": self._trade_stream_dropped,
                "trade_stream_queue_capacity": TRADE_STREAM_QUEUE_SIZE,
                "trade_stream_batch_size": TRADE_STREAM_BATCH_SIZE,
                "trade_sink_concurrency": TRADE_SINK_CONCURRENCY,
                "last_trade_event_time": self._last_trade_event_time,
                "task_states": states,
            },
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=grace_period_seconds)
        await self._exchange.close()

    async def emit_events(self) -> AsyncIterator[Event]:
        return
        yield

    async def handle_event(self, event: Event) -> Optional[EventResponse]:
        return None

    async def backfill_klines(
        self, symbol: str, timeframe: str, limit: int = 500
    ) -> int:
        self._require_initialized()
        klines = await self._exchange.fetch_klines(symbol, timeframe, limit=limit)
        for kline in klines:
            await self._handle_kline(kline)
        return len(klines)

    async def _run_kline_stream(self) -> None:
        while True:
            try:
                async for kline in self._exchange.stream_klines(
                    self._symbols, self._kline_timeframe
                ):
                    await self._handle_kline(kline)
                self._errors += 1
            except asyncio.CancelledError:
                return
            except Exception:
                self._errors += 1
                logger.exception("kline stream crashed; restarting")
            await asyncio.sleep(STREAM_RESTART_DELAY_SECONDS)

    async def _run_trade_stream(self) -> None:
        """Read Binance trades without dropping messages under burst load."""
        while True:
            producer_task: Optional[asyncio.Task] = None
            queue: asyncio.Queue[TradeTick] = asyncio.Queue(
                maxsize=TRADE_STREAM_QUEUE_SIZE
            )
            try:

                async def producer() -> None:
                    async for trade in self._exchange.stream_trades(self._symbols):
                        if queue.full():
                            self._trade_stream_queue_waits += 1
                        # Deliberately await instead of put_nowait: backpressure is
                        # preferable to silently losing market data.
                        await queue.put(trade)
                        self._trade_stream_max_queue_depth = max(
                            self._trade_stream_max_queue_depth, queue.qsize()
                        )

                producer_task = asyncio.create_task(
                    producer(), name="aitos-trade-stream-producer"
                )
                while True:
                    try:
                        first = await asyncio.wait_for(
                            queue.get(), timeout=TRADE_STREAM_IDLE_TIMEOUT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        self._trade_stream_idle_timeouts += 1
                        self._trade_stream_restarts += 1
                        producer_task.cancel()
                        await asyncio.gather(producer_task, return_exceptions=True)
                        producer_task = None
                        await self._recover_recent_trades()
                        break

                    batch = [first]
                    deadline = (
                        asyncio.get_running_loop().time()
                        + TRADE_STREAM_BATCH_WAIT_SECONDS
                    )
                    while len(batch) < TRADE_STREAM_BATCH_SIZE:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            break
                        try:
                            batch.append(await asyncio.wait_for(queue.get(), remaining))
                        except asyncio.TimeoutError:
                            break
                    self._trade_stream_messages_received += len(batch)
                    await self._process_trade_batch(batch)
            except asyncio.CancelledError:
                if producer_task is not None:
                    producer_task.cancel()
                    await asyncio.gather(producer_task, return_exceptions=True)
                return
            except Exception as exc:
                self._errors += 1
                self._trade_stream_errors += 1
                self._trade_stream_restarts += 1
                logger.exception(
                    "trade stream loop crashed; restarting",
                    extra={
                        "aitos_extra": {
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                    },
                )
                if producer_task is not None:
                    producer_task.cancel()
                    await asyncio.gather(producer_task, return_exceptions=True)
                await asyncio.sleep(TRADE_STREAM_RESTART_DELAY_SECONDS)

    async def _process_trade_batch(self, trades: List[TradeTick]) -> None:
        """Update state in wire order, then perform bounded independent I/O."""
        accepted: List[Tuple[TradeTick, Dict[str, Any]]] = []
        for trade in trades:
            previous_id = self._last_trade_ids.get(trade.symbol, -1)
            if trade.trade_id <= previous_id:
                continue
            self._last_trade_ids[trade.symbol] = trade.trade_id
            self._trade_events_received += 1
            self._last_trade_event_time = datetime.now(timezone.utc).isoformat()
            try:
                features = self._live_state.on_trade(trade)
                payload = {
                    "trade_count": features.trade_count,
                    "buy_volume": features.buy_volume,
                    "sell_volume": features.sell_volume,
                    "delta": features.delta,
                    "cvd": features.cvd,
                    "buy_ratio": features.buy_ratio,
                    "aggression": features.aggression,
                    "imbalance": features.imbalance,
                    "bias_score": features.bias_score,
                    "vwap": features.vwap,
                    "last_price": features.last_price,
                    "direction": features.direction,
                    "timestamp": (
                        features.timestamp.isoformat() if features.timestamp else None
                    ),
                }
                self._orderflow_events += 1
                self._ticks_processed += 1
                self._last_event_time = datetime.now(timezone.utc).isoformat()
                accepted.append((trade, payload))
            except Exception as exc:
                self._trade_parse_errors += 1
                self._errors += 1
                logger.exception(
                    "trade state update failed",
                    extra={
                        "aitos_extra": {
                            "symbol": trade.symbol,
                            "trade_id": trade.trade_id,
                            "error": str(exc),
                        }
                    },
                )

        async def io_one(trade: TradeTick, payload: Dict[str, Any]) -> None:
            async with self._trade_sink_semaphore:
                try:
                    jobs = [
                        self._event_bus.publish(
                            Event(
                                topic=trade_topic(trade.symbol),
                                payload=trade.to_dict(),
                                source_module=self.module_id,
                                priority=EventPriority.NORMAL,
                            )
                        ),
                        self._event_bus.publish(
                            Event(
                                topic=orderflow_topic(trade.symbol),
                                payload=payload,
                                source_module=self.module_id,
                                priority=EventPriority.NORMAL,
                            )
                        ),
                    ]
                    if self._repository is not None:
                        jobs.append(self._repository.save_trade_tick(trade))
                    await asyncio.gather(*jobs)
                except Exception as exc:
                    # A sink failure must not kill the exchange reader. Keep the
                    # trade in the bounded pipeline and expose sink failures
                    # separately from websocket/stream failures.
                    self._errors += 1
                    self._trade_downstream_errors += 1
                    logger.exception(
                        "trade downstream processing failed",
                        extra={
                            "aitos_extra": {
                                "symbol": trade.symbol,
                                "trade_id": trade.trade_id,
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                            }
                        },
                    )

        await asyncio.gather(*(io_one(trade, payload) for trade, payload in accepted))
        for trade, _payload in accepted:
            self._publish_live_state(trade.symbol)

    async def _recover_recent_trades(self) -> None:
        """Recover a REST window after a silent websocket gap; IDs prevent duplicates."""
        for symbol in self._symbols:
            try:
                trades = await self._exchange.fetch_recent_trades(
                    symbol, limit=TRADE_FALLBACK_LIMIT
                )
                last_id = self._last_trade_ids.get(symbol, -1)
                fresh = [trade for trade in trades if trade.trade_id > last_id]
                if fresh:
                    await self._process_trade_batch(fresh)
            except Exception as exc:
                self._trade_stream_errors += 1
                logger.exception(
                    "REST trade recovery failed",
                    extra={"aitos_extra": {"symbol": symbol, "error": str(exc)}},
                )

    async def _run_orderbook_stream(self) -> None:
        while True:
            try:
                async for book in self._exchange.stream_order_book(
                    self._symbols, self._orderbook_levels
                ):
                    await self._handle_order_book(book)
                self._errors += 1
            except asyncio.CancelledError:
                return
            except Exception:
                self._errors += 1
                logger.exception("order book stream crashed; restarting")
            await asyncio.sleep(STREAM_RESTART_DELAY_SECONDS)

    async def _handle_kline(self, kline: Kline) -> None:
        await self._event_bus.publish(
            Event(
                topic=kline_topic(kline.symbol, kline.timeframe),
                payload=kline.to_dict(),
                source_module=self.module_id,
                priority=EventPriority.NORMAL,
            )
        )
        if self._repository is not None:
            await self._repository.save_kline(kline)
        self._tick_processed()

    async def _handle_order_book(self, book: OrderBookSnapshot) -> None:
        await self._event_bus.publish(
            Event(
                topic=orderbook_topic(book.symbol),
                payload=book.to_dict(),
                source_module=self.module_id,
                priority=EventPriority.NORMAL,
            )
        )
        if self._repository is not None:
            now = datetime.now(timezone.utc)
            last = self._last_book_persist_at.get(book.symbol)
            if (
                last is None
                or (now - last).total_seconds() >= ORDERBOOK_PERSIST_INTERVAL_SECONDS
            ):
                await self._repository.save_order_book_snapshot(book)
                self._last_book_persist_at[book.symbol] = now
        events = self._live_state.on_order_book(book)
        for event in events:
            await self._event_bus.publish(
                Event(
                    topic=liquidity_topic(book.symbol),
                    payload={
                        "kind": event.kind,
                        "side": event.side,
                        "score": event.score,
                        "price": event.price,
                        "details": event.details,
                        "timestamp": book.timestamp.isoformat(),
                        "last_update_id": book.last_update_id,
                    },
                    source_module=self.module_id,
                    priority=(
                        EventPriority.HIGH
                        if event.kind == "sweep"
                        else EventPriority.NORMAL
                    ),
                )
            )
            self._liquidity_events += 1
        self._publish_live_state(book.symbol)
        self._tick_processed()

    def _publish_live_state(self, symbol: str) -> None:
        state = self._live_state.snapshot(symbol)
        asyncio.create_task(
            self._event_bus.publish(
                Event(
                    topic=live_state_topic(symbol),
                    payload={
                        "trade_count": state.trade_count,
                        "order_flow": (
                            state.order_flow.__dict__ if state.order_flow else None
                        ),
                        "liquidity_events": [
                            e.__dict__ for e in state.liquidity_events[-20:]
                        ],
                        "best_bid": (
                            state.order_book.best_bid if state.order_book else None
                        ),
                        "best_ask": (
                            state.order_book.best_ask if state.order_book else None
                        ),
                        "timestamp": (
                            state.order_book.timestamp.isoformat()
                            if state.order_book
                            else None
                        ),
                    },
                    source_module=self.module_id,
                    priority=EventPriority.NORMAL,
                )
            )
        )

    def _tick_processed(self) -> None:
        self._ticks_processed += 1
        self._last_event_time = datetime.now(timezone.utc).isoformat()

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError(
                "DataIngestionService.initialize() must be called first"
            )
