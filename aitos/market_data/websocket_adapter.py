"""Reusable WebSocket transport helpers for canonical venue adapters."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import websockets

from aitos.logging_setup import get_logger

from .adapter import CanonicalMarketDataAdapter
from .contracts import MarketEvent

logger = get_logger("aitos.market_data.websocket")
Connect = Callable[[str], Awaitable[Any]]
ParserResult = MarketEvent | list[MarketEvent] | None


class JsonWebSocketAdapter(CanonicalMarketDataAdapter):
    """Small transport base shared by venue-specific JSON WebSocket adapters."""

    websocket_url: str
    heartbeat_interval_seconds: float | None = None
    heartbeat_message: str | dict[str, Any] | None = None
    # Exchanges may impose a hard/soft connection lifetime. Closing slightly
    # before that limit lets the reconnect loop establish a fresh connection
    # instead of waiting for an exchange-side forced disconnect.
    max_connection_lifetime_seconds: float | None = None

    def _connect(self, url: str):
        # Venue-level heartbeats are handled explicitly below. Keep protocol
        # ping/pong enabled as a transport-level safety net.
        return websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=10,
            close_timeout=5,
            max_queue=4096,
        )

    async def _heartbeat(self, ws: Any) -> None:
        if self.heartbeat_interval_seconds is None or self.heartbeat_message is None:
            return
        while True:
            await asyncio.sleep(self.heartbeat_interval_seconds)
            payload = self.heartbeat_message
            if isinstance(payload, str):
                await ws.send(payload)
            else:
                await ws.send(json.dumps(payload))

    async def _lifetime_guard(self, ws: Any) -> None:
        lifetime = self.max_connection_lifetime_seconds
        if lifetime is None:
            return
        await asyncio.sleep(lifetime)
        logger.info(
            "canonical websocket lifetime reached; rotating connection",
            extra={"aitos_extra": {"url": self.websocket_url}},
        )
        await ws.close(code=1000, reason="planned connection rotation")

    async def _stream(
        self,
        symbols: list[str],
        subscribe_message: dict[str, Any],
        parser: Callable[[dict[str, Any]], ParserResult],
    ) -> AsyncIterator[MarketEvent]:
        normalized = list(dict.fromkeys(s.upper() for s in symbols))
        if not normalized:
            return
        backoff = 1.0
        while True:
            heartbeat_task: asyncio.Task | None = None
            lifetime_task: asyncio.Task | None = None
            try:
                logger.info(
                    "opening canonical websocket",
                    extra={
                        "aitos_extra": {
                            "url": self.websocket_url,
                            "symbols": normalized,
                        }
                    },
                )
                async with self._connect(self.websocket_url) as ws:
                    await ws.send(json.dumps(subscribe_message))
                    logger.info(
                        "canonical websocket subscription sent",
                        extra={
                            "aitos_extra": {
                                "url": self.websocket_url,
                                "symbols": normalized,
                            }
                        },
                    )
                    if (
                        self.heartbeat_interval_seconds
                        and self.heartbeat_message is not None
                    ):
                        heartbeat_task = asyncio.create_task(self._heartbeat(ws))
                    if self.max_connection_lifetime_seconds is not None:
                        lifetime_task = asyncio.create_task(self._lifetime_guard(ws))
                    backoff = 1.0
                    async for raw in ws:
                        # OKX uses text control frames such as "pong". They
                        # are transport/control messages, not market events.
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")
                        if isinstance(raw, str) and raw.strip().lower() in {
                            "ping",
                            "pong",
                        }:
                            continue
                        try:
                            message = json.loads(raw)
                        except (TypeError, ValueError) as exc:
                            logger.warning(
                                "canonical websocket message decode failed",
                                extra={"aitos_extra": {"error": str(exc)}},
                            )
                            continue
                        if not isinstance(message, dict):
                            continue
                        parsed = parser(message)
                        if parsed is None:
                            continue
                        if isinstance(parsed, list):
                            for event in parsed:
                                yield event
                        else:
                            yield parsed
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "canonical websocket disconnected; reconnecting",
                    extra={
                        "aitos_extra": {
                            "url": self.websocket_url,
                            "error": str(exc),
                            "backoff_seconds": backoff,
                        }
                    },
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 60.0)
            finally:
                for task in (heartbeat_task, lifetime_task):
                    if task is not None and not task.done():
                        task.cancel()
                tasks = [
                    task
                    for task in (heartbeat_task, lifetime_task)
                    if task is not None
                ]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _timestamp_ms(value: Any) -> datetime:
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            return datetime.now(timezone.utc)
        return datetime.fromtimestamp(timestamp / 1000.0, tz=timezone.utc)

    @staticmethod
    def _float(value: Any) -> float:
        return float(value)
