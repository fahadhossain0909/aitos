"""Async Binance Spot WebSocket transport with bounded reconnect behavior."""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

import websockets

_STREAM_RE = re.compile(r"^[a-z0-9]+@[A-Za-z0-9_@]+$")


class SpotOrderBookGap(RuntimeError):
    """Raised when a depth diff cannot be applied to the current snapshot."""


class BinanceSpotWebSocket:
    BASE_URL = "wss://stream.binance.com:9443"

    def __init__(
        self,
        *,
        testnet: bool = True,
        reconnect: bool = True,
        max_backoff_seconds: float = 30.0,
    ) -> None:
        self.base_url = (
            "wss://stream.testnet.binance.vision" if testnet else self.BASE_URL
        )
        self.reconnect = reconnect
        self.max_backoff_seconds = max_backoff_seconds

    async def stream_many(
        self, streams: list[str]
    ) -> AsyncIterator[dict[str, Any]]:
        """Multiplex up to 1024 Binance streams over one WebSocket connection."""
        normalized = list(dict.fromkeys(stream for stream in streams))
        if not normalized or len(normalized) > 1024:
            raise ValueError("combined stream count must be between 1 and 1024")
        if any(not _STREAM_RE.fullmatch(stream) for stream in normalized):
            raise ValueError("invalid Binance combined stream name")
        url = f"{self.base_url}/stream?{urlencode({'streams': '/'.join(normalized)})}"
        delay = 0.5
        while True:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=10,
                    close_timeout=5,
                ) as ws:
                    delay = 0.5
                    async for message in ws:
                        payload = json.loads(message)
                        if isinstance(payload, dict) and "data" in payload:
                            yield payload
                        else:
                            yield {"stream": normalized[0], "data": payload}
                if not self.reconnect:
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                if not self.reconnect:
                    raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.max_backoff_seconds)

    async def stream(
        self, symbol: str, *channels: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Convenience wrapper for one symbol and one or more channels."""
        if not channels:
            raise ValueError("at least one stream channel is required")
        streams = [f"{symbol.lower()}{channel}" for channel in channels]
        async for event in self.stream_many(streams):
            yield event

    async def user_stream(self, listen_key: str) -> AsyncIterator[dict[str, Any]]:
        """Yield private account/order events; caller must keep listen key alive."""
        if not listen_key or "/" in listen_key:
            raise ValueError("invalid listen key")
        url = f"{self.base_url}/ws/{listen_key}"
        delay = 0.5
        while True:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=10,
                    close_timeout=5,
                ) as ws:
                    delay = 0.5
                    async for message in ws:
                        yield json.loads(message)
                if not self.reconnect:
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                if not self.reconnect:
                    raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.max_backoff_seconds)

    async def depth_diffs(self, symbol: str) -> AsyncIterator[dict[str, Any]]:
        """Yield raw depth diffs with continuity checks between consecutive events.

        Bootstrap with GET /api/v3/depth first. The consumer must discard events
        until U <= lastUpdateId + 1 <= u, then require pu == previous u when
        present (or U <= previous u + 1 <= u for older payloads).
        """
        previous_final: int | None = None
        async for event in self.stream(symbol, "@depth@100ms"):
            data = event.get("data", {})
            try:
                first, final = int(data["U"]), int(data["u"])
            except (KeyError, TypeError, ValueError) as exc:
                raise SpotOrderBookGap("malformed depth update") from exc
            if final < first:
                raise SpotOrderBookGap("depth update has invalid sequence range")
            if previous_final is not None:
                previous = data.get("pu")
                if previous is not None and int(previous) != previous_final:
                    raise SpotOrderBookGap("depth update previous sequence mismatch")
                if previous is None and first > previous_final + 1:
                    raise SpotOrderBookGap("depth update sequence gap")
            previous_final = final
            yield event
