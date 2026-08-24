"""DataIngestionService — the glue between exchange streams and AITOS."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

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
TRADE_STREAM_QUEUE_SIZE = 1000
TRADE_FALLBACK_LIMIT = 100
ORDERBOOK_PERSIST_INTERVAL_SECONDS = 1.0


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
    """Live market ingestion plus shared stateful L2 and order-flow intelligence."""

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
        self._exchange, self._event_bus, self._repository = (
            exchange,
            event_bus,
            repository,
        )
        self._symbols, self._kline_timeframe = symbols, kline_timeframe
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
        self._trade_stream_restarts = 0
        self._trade_stream_idle_timeouts = 0
        self._trade_stream_messages_received = 0
        self._last_trade_event_time: Optional[str] = None
        self._last_book_persist_at: Dict[str, datetime] = {}
        self._last_trade_ids: Dict[str, int] = {}
        self._live_state = LiveMarketStateStore(
            max_trades=max(5000, self._liquidity_trade_window)
        )
        self._recent_trades = self._live_state.trades

    @property
    def module_id(self) -> str:
        return "data-ingestion-service"

    @property
    def version(self) -> str:
        return "1.6.0"

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

    async def health_check(self) -> HealthStatus:
        alive = sum(1 for task in self._tasks if not task.done())
        status = (
            ModuleStatus.UNHEALTHY
            if self._errors
            else (
                ModuleStatus.HEALTHY
                if alive == len(self._tasks)
                else ModuleStatus.DEGRADED
            )
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
                "trade_stream_restarts": self._trade_stream_restarts,
                "trade_stream_idle_timeouts": self._trade_stream_idle_timeouts,
                "trade_stream_messages_received": self._trade_stream_messages_received,
                "last_trade_event_time": self._last_trade_event_time,
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
        try:
            async for kline in self._exchange.stream_klines(
                self._symbols, self._kline_timeframe
            ):
                await self._handle_kline(kline)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._errors += 1
            logger.error("kline stream loop crashed: %s", exc)

    async def _run_trade_stream(self) -> None:
        """Consume trades with watchdog plus REST recovery for silent sockets."""
        while True:
            producer_task: Optional[asyncio.Task] = None
            queue: asyncio.Queue[TradeTick] = asyncio.Queue(
                maxsize=TRADE_STREAM_QUEUE_SIZE
            )
            try:

                async def producer() -> None:
                    async for trade in self._exchange.stream_trades(self._symbols):
                        try:
                            queue.put_nowait(trade)
                        except asyncio.QueueFull:
                            self._trade_stream_errors += 1
                            logger.error(
                                "trade stream queue overflow; dropping oldest trade"
                            )
                            try:
                                queue.get_nowait()
                            except asyncio.QueueEmpty:
                                pass
                            queue.put_nowait(trade)

                producer_task = asyncio.create_task(
                    producer(), name="aitos-trade-stream-producer"
                )
                while True:
                    try:
                        trade = await asyncio.wait_for(
                            queue.get(), timeout=TRADE_STREAM_IDLE_TIMEOUT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        self._trade_stream_idle_timeouts += 1
                        self._trade_stream_restarts += 1
                        logger.warning(
                            "trade stream idle timeout; recovering from REST and restarting",
                            extra={
                                "aitos_extra": {
                                    "symbols": self._symbols,
                                    "timeout_seconds": TRADE_STREAM_IDLE_TIMEOUT_SECONDS,
                                    "restart_count": self._trade_stream_restarts,
                                }
                            },
                        )
                        producer_task.cancel()
                        await asyncio.gather(producer_task, return_exceptions=True)
                        producer_task = None
                        await self._recover_recent_trades()
                        break

                    self._trade_stream_messages_received += 1
                    await self._handle_trade(trade)

                await asyncio.sleep(TRADE_STREAM_RESTART_DELAY_SECONDS)
            except asyncio.CancelledError:
                if producer_task is not None:
                    producer_task.cancel()
                    await asyncio.gather(producer_task, return_exceptions=True)
                return
            except Exception as exc:
                self._errors += 1
                self._trade_stream_errors += 1
                self._trade_stream_restarts += 1
                logger.error(
                    "trade stream loop crashed; restarting: %s",
                    exc,
                    extra={
                        "aitos_extra": {
                            "restart_count": self._trade_stream_restarts,
                        },
                    },
                )
                if producer_task is not None:
                    producer_task.cancel()
                    await asyncio.gather(producer_task, return_exceptions=True)
                await asyncio.sleep(TRADE_STREAM_RESTART_DELAY_SECONDS)

    async def _recover_recent_trades(self) -> None:
        """Recover a small REST trade window so order flow stays alive during WS gaps."""
        for symbol in self._symbols:
            try:
                trades = await self._exchange.fetch_recent_trades(
                    symbol, limit=TRADE_FALLBACK_LIMIT
                )
                last_id = self._last_trade_ids.get(symbol, -1)
                fresh = [trade for trade in trades if trade.trade_id > last_id]
                for trade in fresh:
                    await self._handle_trade(trade)
                if fresh:
                    logger.info(
                        "recovered trades from REST",
                        extra={
                            "aitos_extra": {
                                "symbol": symbol,
                                "count": len(fresh),
                            }
                        },
                    )
            except Exception as exc:
                self._trade_stream_errors += 1
                logger.warning(
                    "REST trade recovery failed",
                    extra={"aitos_extra": {"symbol": symbol, "error": str(exc)}},
                )

    async def _run_orderbook_stream(self) -> None:
        try:
            async for book in self._exchange.stream_order_book(
                self._symbols, self._orderbook_levels
            ):
                await self._handle_order_book(book)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._errors += 1
            logger.error("order book stream loop crashed: %s", exc)

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

    async def _handle_trade(self, trade: TradeTick) -> None:
        previous_id = self._last_trade_ids.get(trade.symbol, -1)
        if trade.trade_id <= previous_id:
            return
        self._last_trade_ids[trade.symbol] = trade.trade_id
        self._trade_events_received += 1
        self._last_trade_event_time = datetime.now(timezone.utc).isoformat()
        try:
            await self._event_bus.publish(
                Event(
                    topic=trade_topic(trade.symbol),
                    payload=trade.to_dict(),
                    source_module=self.module_id,
                    priority=EventPriority.NORMAL,
                )
            )
            if self._repository is not None:
                await self._repository.save_trade_tick(trade)
            features = self._live_state.on_trade(trade)
            await self._event_bus.publish(
                Event(
                    topic=orderflow_topic(trade.symbol),
                    payload={
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
                            features.timestamp.isoformat()
                            if features.timestamp
                            else None
                        ),
                    },
                    source_module=self.module_id,
                    priority=EventPriority.NORMAL,
                )
            )
            self._orderflow_events += 1
            self._publish_live_state(trade.symbol)
            self._tick_processed()
        except Exception:
            self._trade_parse_errors += 1
            raise

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
            last_persist = self._last_book_persist_at.get(book.symbol)
            if (
                last_persist is None
                or (now - last_persist).total_seconds()
                >= ORDERBOOK_PERSIST_INTERVAL_SECONDS
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
