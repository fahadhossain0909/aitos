"""Runtime coordinator for the canonical market-data plane."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from aitos.logging_setup import get_logger

from .adapter import CanonicalMarketDataAdapter
from .bus import MarketDataBus
from .contracts import MarketSource
from .gateway import MarketDataGateway

logger = get_logger("aitos.market_data.runtime")
_RECONNECT_INITIAL_DELAY_SECONDS = 1.0
_RECONNECT_MAX_DELAY_SECONDS = 30.0
PUBLISH_RETRY_DELAY_SECONDS = 0.5
DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 30.0
# A single ordered drain worker is intentional: Redis Streams preserve append
# order, and concurrent gateway publishers could reorder trade/order-book events
# before they reach the canonical bus. Throughput improvements should use
# ordered batching/pipelining rather than concurrent per-event publishers.
GATEWAY_DRAIN_WORKERS = 1


class CanonicalMarketDataRuntime:
    """Own exchange sockets and publish normalized events through one gateway."""

    def __init__(
        self,
        adapter: CanonicalMarketDataAdapter,
        market_bus: MarketDataBus,
        gateway: MarketDataGateway,
        symbols: list[str],
        orderbook_levels: int = 20,
        stream_idle_timeout_seconds: float = DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
        enable_trades: bool = True,
        enable_orderbooks: bool = True,
        orderbook_symbols: list[str] | None = None,
    ) -> None:
        if stream_idle_timeout_seconds <= 0:
            raise ValueError("stream_idle_timeout_seconds must be positive")
        if not enable_trades and not enable_orderbooks:
            raise ValueError("at least one market-data stream must be enabled")
        self.adapter = adapter
        self.market_bus = market_bus
        self.gateway = gateway
        self.symbols = list(dict.fromkeys(s.upper() for s in symbols))
        self.orderbook_symbols = list(
            dict.fromkeys(
                s.upper()
                for s in (
                    orderbook_symbols if orderbook_symbols is not None else symbols
                )
            )
        )
        self.orderbook_levels = max(20, orderbook_levels)
        self.stream_idle_timeout_seconds = stream_idle_timeout_seconds
        self.enable_trades = enable_trades
        self.enable_orderbooks = enable_orderbooks
        self._tasks: list[asyncio.Task] = []
        self._drain_tasks: list[asyncio.Task] = []
        self._stopped = True
        self._reconfigure_lock = asyncio.Lock()

    async def start(self) -> None:
        if not self._stopped:
            return
        self._stopped = False
        self.gateway.begin_connect()
        self._drain_tasks = [
            asyncio.create_task(
                self._drain_loop(i), name=f"market-data-gateway-drain-{i}"
            )
            for i in range(GATEWAY_DRAIN_WORKERS)
        ]
        self._tasks = []
        if self.enable_trades and self.symbols:
            self._tasks.append(
                asyncio.create_task(
                    self._run(
                        "trades", lambda: self.adapter.stream_trades(self.symbols)
                    ),
                    name="market-data-trades",
                )
            )
        self._start_orderbook_task()
        logger.info(
            "canonical market-data runtime started",
            extra={
                "aitos_extra": {
                    "venue": self.adapter.venue.value,
                    "market_type": self.adapter.market_type.value,
                    "trade_symbols": self.symbols,
                    "orderbook_symbols": self.orderbook_symbols,
                    "orderbook_levels": self.orderbook_levels,
                    "enable_trades": self.enable_trades,
                    "enable_orderbooks": self.enable_orderbooks,
                    "stream_idle_timeout_seconds": self.stream_idle_timeout_seconds,
                    "gateway_drain_workers": GATEWAY_DRAIN_WORKERS,
                }
            },
        )

    def _start_orderbook_task(self) -> None:
        if not self.enable_orderbooks or not self.orderbook_symbols:
            return
        self._tasks.append(
            asyncio.create_task(
                self._run(
                    "orderbook",
                    lambda: self.adapter.stream_order_books(
                        self.orderbook_symbols, self.orderbook_levels
                    ),
                ),
                name="market-data-orderbook",
            )
        )

    async def update_orderbook_symbols(
        self, symbols: list[str] | tuple[str, ...]
    ) -> bool:
        """Hot-switch the live order-book socket to a new symbol set.

        Trade ingestion remains untouched. The current socket is cancelled and
        closed before the replacement starts, so the gateway never has two live
        order-book subscriptions competing for the same stream.
        """
        normalized = list(dict.fromkeys(s.upper() for s in symbols if s))
        async with self._reconfigure_lock:
            if normalized == self.orderbook_symbols:
                return False
            self.orderbook_symbols = normalized
            if self._stopped:
                return True
            orderbook_tasks = [
                task
                for task in self._tasks
                if task.get_name() == "market-data-orderbook"
            ]
            for task in orderbook_tasks:
                task.cancel()
            if orderbook_tasks:
                await asyncio.gather(*orderbook_tasks, return_exceptions=True)
            self._tasks = [task for task in self._tasks if task not in orderbook_tasks]
            self._start_orderbook_task()
            logger.info(
                "live orderbook subscription reconfigured",
                extra={"aitos_extra": {"orderbook_symbols": normalized}},
            )
            return True

    async def stop(self) -> None:
        self._stopped = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for task in self._drain_tasks:
            task.cancel()
        if self._drain_tasks:
            await asyncio.gather(*self._drain_tasks, return_exceptions=True)
        self._drain_tasks.clear()
        self.gateway.stop()

    async def _drain_loop(self, worker_id: int) -> None:
        while not self._stopped:
            try:
                await self.gateway.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.gateway.health.record_error("publish", str(exc))
                logger.exception(
                    "canonical market-data publish failed; retrying without dropping event",
                    extra={
                        "aitos_extra": {
                            "error": str(exc),
                            "worker_id": worker_id,
                        }
                    },
                )
                await asyncio.sleep(PUBLISH_RETRY_DELAY_SECONDS)

    async def _run(
        self, stream_name: str, stream_factory: Callable[[], AsyncIterator]
    ) -> None:
        delay = _RECONNECT_INITIAL_DELAY_SECONDS
        while not self._stopped:
            saw_event = False
            stream = None
            try:
                stream = stream_factory().__aiter__()
                while not self._stopped:
                    try:
                        event = await asyncio.wait_for(
                            stream.__anext__(), timeout=self.stream_idle_timeout_seconds
                        )
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError as exc:
                        self.gateway.health.record_idle_timeout(
                            stream_name, self.stream_idle_timeout_seconds
                        )
                        self.gateway.mark_reconnecting()
                        logger.error(
                            "canonical market-data stream watchdog timeout; reconnecting",
                            extra={
                                "aitos_extra": {
                                    "stream": stream_name,
                                    "timeout_seconds": self.stream_idle_timeout_seconds,
                                }
                            },
                        )
                        raise exc
                    saw_event = True
                    accepted = await self.gateway.accept_async(event)
                    if accepted and event.source == MarketSource.WEBSOCKET:
                        self.gateway.mark_connected()
                if self._stopped:
                    return
                self.gateway.mark_reconnecting()
                self.gateway.health.record_error(
                    stream_name, "stream ended unexpectedly"
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopped:
                    return
                self.gateway.mark_reconnecting()
                self.gateway.health.record_error(stream_name, str(exc))
                logger.exception(
                    "canonical market-data stream failed; reconnecting",
                    extra={
                        "aitos_extra": {
                            "stream": stream_name,
                            "error": str(exc),
                            "delay": delay,
                        }
                    },
                )
            finally:
                if stream is not None and hasattr(stream, "aclose"):
                    try:
                        await stream.aclose()
                    except Exception:
                        logger.debug("canonical stream close failed", exc_info=True)
            delay = (
                _RECONNECT_INITIAL_DELAY_SECONDS
                if saw_event
                else min(delay * 2, _RECONNECT_MAX_DELAY_SECONDS)
            )
            await asyncio.sleep(delay)
