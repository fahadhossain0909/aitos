"""Runtime coordinator for the canonical market-data plane."""

from __future__ import annotations

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
GATEWAY_DRAIN_WORKERS = 1
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
        exchange = getattr(self.adapter, "exchange", None)
        transport_snapshot = getattr(exchange, "websocket_transport_snapshot", None)
        if transport_snapshot is not None:
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
        """Classify a timeout without changing watchdog thresholds.

        The Binance order-book adapter has an inner bootstrap-readiness timeout.
        That timeout bubbles through ``__anext__`` as the same ``TimeoutError``
        type used by the runtime watchdog, so blindly labelling every timeout
        as ``canonical_idle_timeout`` hides the actual failing stage. Transport
        timestamps let us distinguish a connection that handshook but never
        delivered its first frame from a stream that had already produced data.
        """
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

    async def start(self) -> None:
        if not self._stopped:
            return
        self._stopped = False
        self._first_canonical_event_seen = False
        self._first_canonical_publish_seen = False
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
        self._start_kline_task()
        logger.info(
            "canonical market-data runtime started",
            extra={
                "aitos_extra": {
                    "venue": self.adapter.venue.value,
                    "market_type": self.adapter.market_type.value,
                    "trade_symbols": self.symbols,
                    "orderbook_symbols": self.orderbook_symbols,
                    "kline_symbols": self.kline_symbols,
                    "kline_timeframe": KLINE_TIMEFRAME,
                    "orderbook_levels": self.orderbook_levels,
                    "orderbook_fallback_levels": self.orderbook_fallback_levels,
                    "enable_trades": self.enable_trades,
                    "enable_orderbooks": self.enable_orderbooks,
                    "enable_klines": self.enable_klines,
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

    def _start_kline_task(self) -> None:
        if not self.enable_klines or not self.kline_symbols:
            return
        self._tasks.append(
            asyncio.create_task(
                self._run(
                    "klines",
                    lambda: self.adapter.stream_klines(
                        self.kline_symbols, KLINE_TIMEFRAME
                    ),
                ),
                name="market-data-klines",
            )
        )

    async def update_kline_symbols(self, symbols: list[str] | tuple[str, ...]) -> bool:
        normalized = list(dict.fromkeys(s.upper() for s in symbols if s))[
            :KLINE_SYMBOL_LIMIT
        ]
        async with self._reconfigure_lock:
            if normalized == self.kline_symbols:
                return False
            previous = self.kline_symbols
            self.kline_symbols = normalized
            if self._stopped:
                return True
            kline_tasks = [
                task for task in self._tasks if task.get_name() == "market-data-klines"
            ]
            for task in kline_tasks:
                task.cancel()
            if kline_tasks:
                await asyncio.gather(*kline_tasks, return_exceptions=True)
            self._tasks = [task for task in self._tasks if task not in kline_tasks]
            self._start_kline_task()
            logger.info(
                "live kline subscription reconfigured",
                extra={
                    "aitos_extra": {
                        "stage": "kline_subscription_reconfigured",
                        "previous_symbols": previous,
                        "kline_symbols": normalized,
                        "kline_timeframe": KLINE_TIMEFRAME,
                    }
                },
            )
            return True

    async def update_orderbook_symbols(
        self, symbols: list[str] | tuple[str, ...]
    ) -> bool:
        normalized = list(dict.fromkeys(s.upper() for s in symbols if s))
        async with self._reconfigure_lock:
            if normalized == self.orderbook_symbols:
                return False
            previous = self.orderbook_symbols
            self.orderbook_symbols = normalized
            if self._stopped:
                return True
            await self._restart_orderbook_task()
            logger.info(
                "live orderbook subscription reconfigured",
                extra={
                    "aitos_extra": {
                        "stage": "subscription_reconfigured",
                        "previous_symbols": previous,
                        "orderbook_symbols": normalized,
                        "orderbook_levels": self.orderbook_levels,
                    }
                },
            )
            return True

    async def update_orderbook_levels(self, levels: int, reason: str) -> bool:
        levels = max(20, int(levels))
        async with self._reconfigure_lock:
            if levels == self.orderbook_levels:
                return False
            previous = self.orderbook_levels
            self.orderbook_levels = levels
            if not self._stopped:
                await self._restart_orderbook_task()
            logger.warning(
                "live orderbook depth changed",
                extra={
                    "aitos_extra": {
                        "stage": "orderbook_depth_reconfigured",
                        "previous_levels": previous,
                        "orderbook_levels": levels,
                        "reason": reason,
                    }
                },
            )
            return True

    async def _restart_orderbook_task(self) -> None:
        orderbook_tasks = [
            task for task in self._tasks if task.get_name() == "market-data-orderbook"
        ]
        for task in orderbook_tasks:
            task.cancel()
        if orderbook_tasks:
            await asyncio.gather(*orderbook_tasks, return_exceptions=True)
        self._tasks = [task for task in self._tasks if task not in orderbook_tasks]
        self._start_orderbook_task()

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
                published = await self.gateway.drain_once()
                if published and not self._first_canonical_publish_seen:
                    self._first_canonical_publish_seen = True
                    logger.info(
                        "canonical market-data first publish completed",
                        extra={
                            "aitos_extra": {
                                "stage": "first_canonical_publish",
                                "worker_id": worker_id,
                            }
                        },
                    )
                await self._adapt_orderbook_depth()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "canonical market-data publish failed; dropping failed event and continuing",
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

    def _stream_state(self, stream_name: str) -> dict[str, object]:
        return self._stream_telemetry.setdefault(
            stream_name,
            {
                "restarts": 0,
                "idle_timeouts": 0,
                "bootstrap_ready_timeouts": 0,
                "errors": 0,
                "events": 0,
                "accepted": 0,
                "last_start_at": None,
                "last_event_at": None,
                "last_failure_at": None,
                "consecutive_failures": 0,
                "last_failure_kind": None,
            },
        )

    async def _run(
        self, stream_name: str, stream_factory: Callable[[], AsyncIterator]
    ) -> None:
        delay = _RECONNECT_INITIAL_DELAY_SECONDS
        state = self._stream_state(stream_name)
        while not self._stopped:
            saw_event = False
            stream = None
            started_monotonic = time.monotonic()
            failure_kind: str | None = None
            try:
                state["last_start_at"] = time.time()
                logger.info(
                    "canonical stream starting",
                    extra={
                        "aitos_extra": {
                            "stage": "stream_start",
                            "stream": stream_name,
                            "reconnect_delay": delay,
                            "consecutive_failures": state["consecutive_failures"],
                        }
                    },
                )
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
                        failure_kind, transport = self._classify_timeout(stream_name)
                        if failure_kind == "bootstrap_ready_timeout":
                            state["bootstrap_ready_timeouts"] = (
                                int(state["bootstrap_ready_timeouts"]) + 1
                            )
                        else:
                            state["idle_timeouts"] = int(state["idle_timeouts"]) + 1
                        logger.error(
                            "canonical market-data timeout; reconnecting",
                            extra={
                                "aitos_extra": {
                                    "stage": (
                                        "orderbook_bootstrap_ready_timeout"
                                        if failure_kind == "bootstrap_ready_timeout"
                                        else "canonical_idle_timeout"
                                    ),
                                    "stream": stream_name,
                                    "timeout_seconds": self.stream_idle_timeout_seconds,
                                    "consecutive_failures": state[
                                        "consecutive_failures"
                                    ],
                                    "failure_kind": failure_kind,
                                    "transport": {
                                        key: transport.get(key)
                                        for key in (
                                            "state",
                                            "current_url",
                                            "last_connect_started_at",
                                            "last_handshake_at",
                                            "last_first_frame_at",
                                            "last_market_event_at",
                                            "last_error_type",
                                            "last_error",
                                        )
                                    },
                                }
                            },
                        )
                        raise exc
                    saw_event = True
                    state["events"] = int(state["events"]) + 1
                    accepted = await self.gateway.accept_async(event)
                    if accepted:
                        state["accepted"] = int(state["accepted"]) + 1
                    state["last_event_at"] = time.time()
                    if accepted and event.source == MarketSource.WEBSOCKET:
                        if not self._first_canonical_event_seen:
                            self._first_canonical_event_seen = True
                            logger.info(
                                "canonical market-data first event accepted",
                                extra={
                                    "aitos_extra": {
                                        "stage": "first_canonical_event",
                                        "stream": stream_name,
                                        "event_type": type(event).__name__,
                                        "source": event.source.value,
                                    }
                                },
                            )
                        self.gateway.mark_connected()
                if self._stopped:
                    return
                if failure_kind is None:
                    failure_kind = "stream_end"
                self.gateway.mark_reconnecting()
                self.gateway.health.record_error(
                    stream_name, "stream ended unexpectedly"
                )
                state["errors"] = int(state["errors"]) + 1
                state["last_failure_kind"] = failure_kind
                logger.warning(
                    "canonical stream ended unexpectedly",
                    extra={
                        "aitos_extra": {
                            "stage": "canonical_stream_end",
                            "stream": stream_name,
                        }
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopped:
                    return
                if failure_kind is None:
                    failure_kind = type(exc).__name__
                self.gateway.mark_reconnecting()
                self.gateway.health.record_error(stream_name, str(exc))
                state["errors"] = int(state["errors"]) + 1
                state["last_failure_at"] = time.time()
                state["last_failure_kind"] = failure_kind
                logger.exception(
                    "canonical market-data stream failed; reconnecting",
                    extra={
                        "aitos_extra": {
                            "stage": "canonical_stream_error",
                            "stream": stream_name,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "delay": delay,
                            "failure_kind": failure_kind,
                        }
                    },
                )
            finally:
                if stream is not None and hasattr(stream, "aclose"):
                    try:
                        await stream.aclose()
                    except Exception:
                        logger.debug("canonical stream close failed", exc_info=True)
            elapsed = time.monotonic() - started_monotonic
            if self._stopped:
                return
            if (
                failure_kind
                in {
                    "idle_timeout",
                    "bootstrap_ready_timeout",
                    "stream_end",
                }
                or not saw_event
            ):
                state["consecutive_failures"] = int(state["consecutive_failures"]) + 1
                delay = min(
                    max(
                        delay * 2 if int(state["consecutive_failures"]) > 1 else delay,
                        _RECONNECT_INITIAL_DELAY_SECONDS,
                    ),
                    _RECONNECT_MAX_DELAY_SECONDS,
                )
            else:
                if elapsed >= RECONNECT_STABLE_SECONDS:
                    state["consecutive_failures"] = 0
                    delay = _RECONNECT_INITIAL_DELAY_SECONDS
                else:
                    state["consecutive_failures"] = (
                        int(state["consecutive_failures"]) + 1
                    )
                    delay = min(
                        max(delay * 2, _RECONNECT_INITIAL_DELAY_SECONDS),
                        _RECONNECT_MAX_DELAY_SECONDS,
                    )
            state["restarts"] = int(state["restarts"]) + 1
            logger.warning(
                "canonical stream reconnect scheduled",
                extra={
                    "aitos_extra": {
                        "stage": "canonical_reconnect_scheduled",
                        "stream": stream_name,
                        "failure_kind": failure_kind,
                        "delay": delay,
                        "consecutive_failures": state["consecutive_failures"],
                        "stream_uptime_seconds": round(elapsed, 3),
                    }
                },
            )
            await asyncio.sleep(delay)
