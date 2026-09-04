"""Reusable WebSocket transport helpers for canonical venue adapters."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import websockets

from .adapter import CanonicalMarketDataAdapter
from .contracts import MarketEvent

Connect = Callable[[str], Awaitable[Any]]


class JsonWebSocketAdapter(CanonicalMarketDataAdapter):
    """Small transport base shared by venue-specific JSON WebSocket adapters.

    Venue adapters provide endpoint construction, subscription messages, and
    payload parsing. The base deliberately owns no venue semantics.
    """

    websocket_url: str

    def _connect(self, url: str):
        return websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=10,
            close_timeout=5,
            max_queue=4096,
        )

    async def _stream(
        self,
        symbols: list[str],
        subscribe_message: dict[str, Any],
        parser: Callable[[dict[str, Any]], MarketEvent | None],
    ) -> AsyncIterator[MarketEvent]:
        normalized = list(dict.fromkeys(s.upper() for s in symbols))
        if not normalized:
            return
        async with self._connect(self.websocket_url) as ws:
            await ws.send(json.dumps(subscribe_message))
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                message = json.loads(raw)
                if not isinstance(message, dict):
                    continue
                event = parser(message)
                if event is not None:
                    yield event

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
