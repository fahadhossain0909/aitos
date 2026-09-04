"""Reusable WebSocket transport helpers for canonical venue adapters."""

from __future__ import annotations

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
        parser: Callable[[dict[str, Any]], ParserResult],
    ) -> AsyncIterator[MarketEvent]:
        normalized = list(dict.fromkeys(s.upper() for s in symbols))
        if not normalized:
            return
        logger.info(
            "opening canonical websocket",
            extra={"aitos_extra": {"url": self.websocket_url, "symbols": normalized}},
        )
        async with self._connect(self.websocket_url) as ws:
            await ws.send(json.dumps(subscribe_message))
            logger.info(
                "canonical websocket subscription sent",
                extra={
                    "aitos_extra": {"url": self.websocket_url, "symbols": normalized}
                },
            )
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
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
