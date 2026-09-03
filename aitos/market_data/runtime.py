"""Runtime coordinator for the canonical market-data plane."""

from __future__ import annotations

import asyncio

from aitos.logging_setup import get_logger

from .binance_adapter import BinanceCanonicalMarketDataAdapter
from .bus import MarketDataBus
from .gateway import MarketDataGateway

logger = get_logger("aitos.market_data.runtime")


class CanonicalMarketDataRuntime:
    """Own exchange sockets and publish normalized events into one gateway.

    Consumers never open exchange sockets themselves. A failure in one stream
    is isolated and reported by the gateway rather than taking the scanner down.
    """

    def __init__(
        self,
        adapter: BinanceCanonicalMarketDataAdapter,
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
        self._stopped = True

    async def start(self) -> None:
        if not self._stopped:
            return
        self._stopped = False
        await self.gateway.start()
        self._tasks = [
            asyncio.create_task(
                self._run("trades", self.adapter.stream_trades(self.symbols))
            ),
            asyncio.create_task(
                self._run(
                    "orderbook",
                    self.adapter.stream_order_books(
                        self.symbols, self.orderbook_levels
                    ),
                )
            ),
        ]
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
        await self.gateway.stop()

    async def _run(self, stream_name: str, stream) -> None:
        try:
            async for event in stream:
                accepted = await self.gateway.accept(event)
                if accepted:
                    await self.market_bus.publish(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.gateway.health.record_error(f"{stream_name}: {exc}")
            logger.exception(
                "canonical market-data stream stopped",
                extra={"aitos_extra": {"stream": stream_name, "error": str(exc)}},
            )
            # Do not restart recursively here. The exchange adapter owns socket
            # reconnects; a terminal stream failure must remain observable.
