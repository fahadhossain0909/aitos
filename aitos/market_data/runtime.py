"""Runtime coordinator for the canonical market-data plane."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from aitos.logging_setup import get_logger

from .adapter import CanonicalMarketDataAdapter
from .bus import MarketDataBus
from .gateway import MarketDataGateway

logger = get_logger("aitos.market_data.runtime")

_RECONNECT_INITIAL_DELAY_SECONDS = 1.0
_RECONNECT_MAX_DELAY_SECONDS = 30.0


class CanonicalMarketDataRuntime:
    """Own exchange sockets and publish normalized events through one gateway.

    A stream ending or raising is treated as a transport failure, not as a
    successful shutdown. The runtime recreates the adapter stream with bounded
    exponential backoff until ``stop()`` is requested.
    """

    def __init__(
        self,
        adapter: CanonicalMarketDataAdapter,
        market_bus: MarketDataBus,
        gateway: MarketDataGateway,
        symbols: list[str],
        orderbook_levels: int = 20,
    ) -> None:
        self.adapter = adapter
        self.market_bus = market_bus
        self.gateway = gateway
        self.symbols = list(dict.fromkeys(s.upper() for s in symbols))
        self.orderbook_levels = orderbook_levels
        self._tasks: list[asyncio.Task] = []
        self._drain_task: asyncio.Task | None = None
        self._stopped = True

    async def start(self) -> None:
        if not self._stopped:
            return
        self._stopped = False
        self.gateway.begin_connect()
        self._drain_task = asyncio.create_task(
            self._drain_loop(), name="market-data-gateway-drain"
        )
        self._tasks = [
            asyncio.create_task(
                self._run("trades", lambda: self.adapter.stream_trades(self.symbols)),
                name="market-data-trades",
            ),
            asyncio.create_task(
                self._run(
                    "orderbook",
                    lambda: self.adapter.stream_order_books(
                        self.symbols, self.orderbook_levels
                    ),
                ),
                name="market-data-orderbook",
            ),
        ]
        self.gateway.mark_connected()
        logger.info(
            "canonical market-data runtime started",
            extra={"aitos_extra": {"symbols": self.symbols}},
        )

    async def stop(self) -> None:
        self._stopped = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._drain_task is not None:
            self._drain_task.cancel()
            await asyncio.gather(self._drain_task, return_exceptions=True)
            self._drain_task = None
        self.gateway.stop()

    async def _drain_loop(self) -> None:
        while not self._stopped:
            try:
                await self.gateway.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.gateway.health.record_error("publish", str(exc))
                logger.exception(
                    "canonical market-data publish failed",
                    extra={"aitos_extra": {"error": str(exc)}},
                )

    async def _run(
        self,
        stream_name: str,
        stream_factory: Callable[[], AsyncIterator],
    ) -> None:
        delay = _RECONNECT_INITIAL_DELAY_SECONDS
        while not self._stopped:
            saw_event = False
            try:
                async for event in stream_factory():
                    saw_event = True
                    self.gateway.accept(event)
                if self._stopped:
                    return
                self.gateway.mark_reconnecting()
                self.gateway.health.record_error(
                    stream_name, "stream ended unexpectedly"
                )
                logger.warning(
                    "canonical market-data stream ended; reconnecting",
                    extra={"aitos_extra": {"stream": stream_name, "delay": delay}},
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
            if saw_event:
                delay = _RECONNECT_INITIAL_DELAY_SECONDS
            else:
                delay = min(delay * 2, _RECONNECT_MAX_DELAY_SECONDS)
            await asyncio.sleep(delay)
