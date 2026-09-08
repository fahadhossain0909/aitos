"""Runtime for non-trade/non-book canonical market-data producers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from aitos.logging_setup import get_logger

from .binance_auxiliary import BinanceAuxiliaryMarketDataAdapter
from .contracts import MarketSource
from .gateway import MarketDataGateway

logger = get_logger("aitos.market_data.auxiliary_runtime")


class BinanceAuxiliaryMarketDataRuntime:
    """Run bounded ticker/funding/OI plus global liquidation and instrument feeds."""

    def __init__(
        self,
        adapter: BinanceAuxiliaryMarketDataAdapter,
        gateway: MarketDataGateway,
        symbols: list[str],
    ) -> None:
        self.adapter = adapter
        self.gateway = gateway
        self.symbols = list(dict.fromkeys(s.upper() for s in symbols if s))[:50]
        self._tasks: list[asyncio.Task] = []
        self._drain_tasks: list[asyncio.Task] = []
        self._stopped = True
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if not self._stopped:
            return
        self._stopped = False
        self.gateway.begin_connect()
        self._drain_tasks = [
            asyncio.create_task(self._drain_loop(), name="market-data-auxiliary-drain")
        ]
        self._start("ticker", lambda: self.adapter.stream_tickers(self.symbols))
        self._start("funding", lambda: self.adapter.stream_funding(self.symbols))
        self._start(
            "open_interest", lambda: self.adapter.stream_open_interest(self.symbols)
        )
        self._start("liquidation", lambda: self.adapter.stream_liquidations())
        self._start("instrument", lambda: self.adapter.stream_instruments(self.symbols))
        logger.info(
            "Binance auxiliary market-data runtime started",
            extra={"aitos_extra": {"symbols": self.symbols}},
        )

    def _start(self, name: str, factory: Callable) -> None:
        self._tasks.append(
            asyncio.create_task(self._run(name, factory), name=f"market-data-{name}")
        )

    async def _drain_loop(self) -> None:
        while not self._stopped:
            try:
                await self.gateway.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "auxiliary gateway drain failed",
                    extra={"aitos_extra": {"error": str(exc)}},
                )
                await asyncio.sleep(0.05)

    async def _run(self, name: str, factory: Callable) -> None:
        delay = 1.0
        while not self._stopped:
            stream = None
            try:
                stream = factory().__aiter__()
                while not self._stopped:
                    event = await stream.__anext__()
                    if event.source == MarketSource.WEBSOCKET:
                        self.gateway.mark_connected()
                    await self.gateway.accept_async(event)
                    delay = 1.0
            except StopAsyncIteration:
                pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._stopped:
                    self.gateway.mark_reconnecting()
                    logger.warning(
                        "auxiliary market-data producer failed",
                        extra={
                            "aitos_extra": {
                                "stream": name,
                                "error": str(exc),
                                "delay": delay,
                            }
                        },
                    )
            finally:
                if stream is not None and hasattr(stream, "aclose"):
                    await stream.aclose()
            if not self._stopped:
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, 30.0)

    async def update_symbols(self, symbols: list[str] | tuple[str, ...]) -> bool:
        normalized = list(dict.fromkeys(s.upper() for s in symbols if s))[:50]
        async with self._lock:
            if normalized == self.symbols:
                return False
            self.symbols = normalized
            if self._stopped:
                return True
            names = {"ticker", "funding", "open_interest"}
            tasks = [
                t
                for t in self._tasks
                if t.get_name().removeprefix("market-data-") in names
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks = [t for t in self._tasks if t not in tasks]
            self._start("ticker", lambda: self.adapter.stream_tickers(self.symbols))
            self._start("funding", lambda: self.adapter.stream_funding(self.symbols))
            self._start(
                "open_interest", lambda: self.adapter.stream_open_interest(self.symbols)
            )
            return True

    async def stop(self) -> None:
        self._stopped = True
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for task in self._drain_tasks:
            task.cancel()
        if self._drain_tasks:
            await asyncio.gather(*self._drain_tasks, return_exceptions=True)
        self._drain_tasks.clear()
        self.gateway.stop()

    def snapshot(self) -> dict[str, object]:
        return {
            "running": not self._stopped,
            "symbols": list(self.symbols),
            "active_producers": [t.get_name() for t in self._tasks if not t.done()],
            "gateway": self.gateway.snapshot(),
        }
