"""Runtime coordinator for the canonical market-data plane."""

from __future__ import annotations

# Keep the runtime coordinator in the canonical market-data module; ingestion
# and scanner reconfiguration depend on this public class and its lifecycle API.
import asyncio
import time
from collections.abc import AsyncIterator, Callable

from aitos.logging_setup import get_logger

from .adapter import CanonicalMarketDataAdapter
from .bus import MarketDataBus
from .contracts import MarketSource
from .gateway import MarketDataGateway

logger = get_logger("aitos.market_data.runtime")
_RECONNECT_INITIAL_DELAY_SECONDS = 1.0
_RECONNECT_MAX_DELAY_SECONDS = 30.0
RECONNECT_STABLE_SECONDS = 10.0
PUBLISH_RETRY_DELAY_SECONDS = 0.05
DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 30.0
GATEWAY_DRAIN_WORKERS = 4
DEPTH_PRESSURE_THRESHOLD = 100
KLINE_TIMEFRAME = "1m"
KLINE_SYMBOL_LIMIT = 5


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
        orderbook_fallback_levels: int = 100,
        kline_symbols: list[str] | None = None,
        enable_klines: bool = False,
    ) -> None:
        if stream_idle_timeout_seconds <= 0:
            raise ValueError("stream_idle_timeout_seconds must be positive")
        if not enable_trades and not enable_orderbooks and not enable_klines:
            raise ValueError("at least one market-data stream must be enabled")
        if orderbook_fallback_levels <= 0:
            raise ValueError("orderbook_fallback_levels must be positive")
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
        self.kline_symbols = list(
            dict.fromkeys(s.upper() for s in (kline_symbols or []))
        )[:KLINE_SYMBOL_LIMIT]
        self.orderbook_levels = max(20, orderbook_levels)
        self.orderbook_fallback_levels = max(
            20, min(orderbook_fallback_levels, self.orderbook_levels)
        )
        self.stream_idle_timeout_seconds = stream_idle_timeout_seconds
        self.enable_trades = enable_trades
        self.enable_orderbooks = enable_orderbooks
        self.enable_klines = enable_klines
        self._tasks: list[asyncio.Task] = []
        self._drain_tasks: list[asyncio.Task] = []
        self._stopped = True
        self._reconfigure_lock = asyncio.Lock()
        self._depth_fallback_applied = False
        self._first_canonical_event_seen = False
        self._first_canonical_publish_seen = False
        self._stream_telemetry: dict[str, dict[str, object]] = {}
        self._trade_stream: object | None = None
        self._kline_stream: object | None = None
        self._orderbook_stream: object | None = None
        exchange = getattr(self.adapter, "exchange", None)
        if getattr(exchange, "websocket_transport_snapshot", None) is not None:
            self.gateway._transport_snapshot_provider = self._transport_snapshot

    def _transport_snapshot(self) -> dict[str, object]:
        exchange = getattr(self.adapter, "exchange", None)
        snapshot = (
            dict(exchange.websocket_transport_snapshot())
            if exchange is not None
            and hasattr(exchange, "websocket_transport_snapshot")
            else {}
        )
        snapshot["runtime_streams"] = {
            name: dict(value) for name, value in self._stream_telemetry.items()
        }
        return snapshot

    def _classify_timeout(self, stream_name: str) -> tuple[str, dict[str, object]]:
        exchange = getattr(self.adapter, "exchange", None)
        snapshot = (
            dict(exchange.websocket_transport_snapshot())
            if exchange is not None
            and hasattr(exchange, "websocket_transport_snapshot")
            else {}
        )
        if stream_name != "orderbook":
            return "idle_timeout", snapshot
        handshake = snapshot.get("last_handshake_at")
        first_frame = snapshot.get("last_first_frame_at")
        if handshake and (not first_frame or str(first_frame) < str(handshake)):
            return "bootstrap_ready_timeout", snapshot
        return "idle_timeout", snapshot

    def _stream_state(self, stream_name: str) -> dict[str, object]:
        return self._stream_telemetry.setdefault(
            stream_name,
            {
                "restarts": 0,
                "idle_timeouts": 0,
                "errors": 0,
                "events": 0,
                "accepted": 0,
                "last_start_at": None,
                "last_event_at": None,
                "last_failure_at": None,
                "last_failure_kind": None,
                "last_failure_elapsed_seconds": 0.0,
                "consecutive_failures": 0,
            },
        )

    async def start(self) -> None:
        if not self._stopped:
            return
        self._stopped = False
        self._first_canonical_event_seen = False
        self._first_canonical_publish_seen = False
        self._trade_stream = None
        self._kline_stream = None
        self._orderbook_stream = None
        self.gateway.begin_connect()
        self._drain_tasks = [
            asyncio.create_task(
                self._drain_loop(i), name=f"market-data-gateway-drain-{i}"
            )
            for i in range(GATEWAY_DRAIN_WORKERS)
        ]
        self._tasks = []
        self._start_trades_task()
        self._start_orderbook_task()
        self._start_kline_task()

    # -- trades ---------------------------------------------------------

    def _start_trades_task(self) -> None:
        if not self.enable_trades or not self.symbols:
            return
        self._tasks.append(
            asyncio.create_task(
                self._run("trades", self._trade_stream_factory),
                name="market-data-trades",
            )
        )

    def _trade_stream_factory(self) -> AsyncIterator:
        if hasattr(self.adapter, "stream_trades_managed"):
            return self._managed_trade_stream_gen()
        self._trade_stream = None
        return self.adapter.stream_trades(self.symbols)

    async def _managed_trade_stream_gen(self) -> AsyncIterator:
        stream = await self.adapter.stream_trades_managed(self.symbols)
        self._trade_stream = stream
        try:
            async for event in stream:
                yield event
        finally:
            self._trade_stream = None
            await stream.aclose()

    async def update_trade_symbols(self, symbols: list[str] | tuple[str, ...]) -> bool:
        """Reconfigure the live trade cohort.

        When the adapter supports managed streams, this updates the
        existing websocket subscription in place (SUBSCRIBE/UNSUBSCRIBE on
        the live connection) -- it does not cancel or reconnect anything,
        so routine churn (a position opening or closing, the scanner's
        ranking shifting) no longer costs a reconnect, an orderbook
        re-bootstrap, or a freshness gap.
        """
        normalized = list(dict.fromkeys(s.upper() for s in symbols if s))
        async with self._reconfigure_lock:
            if normalized == self.symbols:
                return False
            self.symbols = normalized
            if self._stopped:
                return True
            if self._trade_stream is not None and hasattr(
                self._trade_stream, "update_symbols"
            ):
                await self._trade_stream.update_symbols(normalized)
                return True
            tasks = [t for t in self._tasks if t.get_name() == "market-data-trades"]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks = [t for t in self._tasks if t not in tasks]
            self._start_trades_task()
            return True

    # -- orderbook --------------------------------------------------------

    def _start_orderbook_task(self) -> None:
        if not self.enable_orderbooks or not self.orderbook_symbols:
            return
        self._tasks.append(
            asyncio.create_task(
                self._run("orderbook", self._orderbook_stream_factory),
                name="market-data-orderbook",
            )
        )

    def _orderbook_stream_factory(self) -> AsyncIterator:
        if hasattr(self.adapter, "stream_order_books_managed"):
            return self._managed_orderbook_stream_gen()
        self._orderbook_stream = None
        return self.adapter.stream_order_books(
            self.orderbook_symbols, self.orderbook_levels
        )

    async def _managed_orderbook_stream_gen(self) -> AsyncIterator:
        stream = await self.adapter.stream_order_books_managed(
            self.orderbook_symbols, levels=self.orderbook_levels
        )
        self._orderbook_stream = stream
        try:
            async for event in stream:
                yield event
        finally:
            self._orderbook_stream = None
            await stream.aclose()

    def _start_kline_task(self) -> None:
        if not self.enable_klines or not self.kline_symbols:
            return
        self._tasks.append(
            asyncio.create_task(
                self._run("klines", self._kline_stream_factory),
                name="market-data-klines",
            )
        )

    def _kline_stream_factory(self) -> AsyncIterator:
        if hasattr(self.adapter, "stream_klines_managed"):
            return self._managed_kline_stream_gen()
        self._kline_stream = None
        return self.adapter.stream_klines(self.kline_symbols, KLINE_TIMEFRAME)

    async def _managed_kline_stream_gen(self) -> AsyncIterator:
        stream = await self.adapter.stream_klines_managed(
            self.kline_symbols, KLINE_TIMEFRAME
        )
        self._kline_stream = stream
        try:
            async for event in stream:
                yield event
        finally:
            self._kline_stream = None
            await stream.aclose()

    async def update_kline_symbols(self, symbols: list[str] | tuple[str, ...]) -> bool:
        normalized = list(dict.fromkeys(s.upper() for s in symbols if s))[
            :KLINE_SYMBOL_LIMIT
        ]
        async with self._reconfigure_lock:
            if normalized == self.kline_symbols:
                return False
            self.kline_symbols = normalized
            if self._stopped:
                return True
            if self._kline_stream is not None and hasattr(
                self._kline_stream, "update_symbols"
            ):
                await self._kline_stream.update_symbols(normalized)
                return True
            tasks = [t for t in self._tasks if t.get_name() == "market-data-klines"]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks = [t for t in self._tasks if t not in tasks]
            self._start_kline_task()
            return True

    async def update_orderbook_symbols(
        self, symbols: list[str] | tuple[str, ...]
    ) -> bool:
        normalized = list(dict.fromkeys(s.upper() for s in symbols if s))
        async with self._reconfigure_lock:
            if normalized == self.orderbook_symbols:
                return False
            self.orderbook_symbols = normalized
            if self._stopped:
                return True
            if self._orderbook_stream is not None and hasattr(
                self._orderbook_stream, "update_symbols"
            ):
                await self._orderbook_stream.update_symbols(normalized)
                return True
            await self._restart_orderbook_task()
            return True

    async def update_orderbook_levels(self, levels: int, reason: str) -> bool:
        levels = max(20, int(levels))
        async with self._reconfigure_lock:
            if levels == self.orderbook_levels:
                return False
            self.orderbook_levels = levels
            if not self._stopped:
                if self._orderbook_stream is not None and hasattr(
                    self._orderbook_stream, "update_levels"
                ):
                    # Depth truncation is purely local -- Binance always
                    # sends full depth diffs regardless of `levels`, so this
                    # never needs to touch the websocket at all.
                    await self._orderbook_stream.update_levels(levels)
                else:
                    await self._restart_orderbook_task()
            return True

    async def _restart_orderbook_task(self) -> None:
        tasks = [t for t in self._tasks if t.get_name() == "market-data-orderbook"]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks = [t for t in self._tasks if t not in tasks]
        self._start_orderbook_task()

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

    async def _drain_loop(self, worker_id: int) -> None:
        while not self._stopped:
            try:
                published = await self.gateway.drain_once()
                if published and not self._first_canonical_publish_seen:
                    self._first_canonical_publish_seen = True
                await self._adapt_orderbook_depth()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "canonical market-data publish failed; continuing",
                    extra={
                        "aitos_extra": {
                            "stage": "canonical_publish_error",
                            "error": str(exc),
                            "worker_id": worker_id,
                        }
                    },
                )
                await asyncio.sleep(PUBLISH_RETRY_DELAY_SECONDS)

    async def _adapt_orderbook_depth(self) -> None:
        if self._depth_fallback_applied:
            return
        queue = self.gateway.snapshot().get("queue", {})
        replaced = int(queue.get("replaced_oldest", 0))
        freshness_drops = int(self.gateway.health.freshness_drops)
        if (
            self.orderbook_levels > self.orderbook_fallback_levels
            and max(replaced, freshness_drops) >= DEPTH_PRESSURE_THRESHOLD
        ):
            self._depth_fallback_applied = True
            await self.update_orderbook_levels(
                self.orderbook_fallback_levels,
                reason=f"live-path pressure detected: replaced_oldest={replaced}, freshness_drops={freshness_drops}",
            )

    async def _run(
        self, stream_name: str, stream_factory: Callable[[], AsyncIterator]
    ) -> None:
        delay = _RECONNECT_INITIAL_DELAY_SECONDS
        state = self._stream_state(stream_name)
        while not self._stopped:
            saw_event = False
            stream = None
            started = time.monotonic()
            failure_kind: str | None = None
            try:
                state["last_start_at"] = time.time()
                stream = stream_factory().__aiter__()
                while not self._stopped:
                    try:
                        event = await asyncio.wait_for(
                            stream.__anext__(), timeout=self.stream_idle_timeout_seconds
                        )
                    except StopAsyncIteration:
                        failure_kind = "stream_end"
                        break
                    except asyncio.TimeoutError as exc:
                        failure_kind, _ = self._classify_timeout(stream_name)
                        state["idle_timeouts"] = int(state["idle_timeouts"]) + 1
                        raise exc
                    saw_event = True
                    state["events"] = int(state["events"]) + 1
                    accepted = await self.gateway.accept_async(event)
                    if accepted:
                        state["accepted"] = int(state["accepted"]) + 1
                    state["last_event_at"] = time.time()
                    if accepted and event.source == MarketSource.WEBSOCKET:
                        self._first_canonical_event_seen = True
                        self.gateway.mark_connected()
                if self._stopped:
                    return
                failure_kind = failure_kind or "stream_end"
                self.gateway.mark_reconnecting()
                self.gateway.health.record_error(
                    stream_name, "stream ended unexpectedly"
                )
                state["errors"] = int(state["errors"]) + 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopped:
                    return
                failure_kind = failure_kind or type(exc).__name__
                self.gateway.mark_reconnecting()
                self.gateway.health.record_error(stream_name, str(exc))
                state["errors"] = int(state["errors"]) + 1
                state["last_failure_at"] = time.time()
            finally:
                if stream is not None and hasattr(stream, "aclose"):
                    try:
                        await stream.aclose()
                    except Exception:
                        logger.debug("canonical stream close failed", exc_info=True)
            elapsed = time.monotonic() - started
            if self._stopped:
                return
            state["last_failure_kind"] = failure_kind
            state["last_failure_elapsed_seconds"] = round(elapsed, 3)
            logger.warning(
                "canonical market-data stream restarting",
                extra={
                    "aitos_extra": {
                        "stage": "canonical_stream_restart",
                        "stream": stream_name,
                        "failure_kind": failure_kind,
                        "elapsed_seconds": round(elapsed, 3),
                        "saw_event": saw_event,
                        "idle_timeout_seconds": self.stream_idle_timeout_seconds,
                        "orderbook_symbols": (
                            list(self.orderbook_symbols)
                            if stream_name == "orderbook"
                            else None
                        ),
                    }
                },
            )
            state["restarts"] = int(state["restarts"]) + 1
            sleep_delay = delay
            if elapsed >= RECONNECT_STABLE_SECONDS and saw_event:
                state["consecutive_failures"] = 0
                delay = _RECONNECT_INITIAL_DELAY_SECONDS
            else:
                state["consecutive_failures"] = int(state["consecutive_failures"]) + 1
                delay = min(_RECONNECT_MAX_DELAY_SECONDS, delay * 2)
            await asyncio.sleep(sleep_delay)
