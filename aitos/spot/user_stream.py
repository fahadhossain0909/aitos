"""Supervised Binance Spot private User Data Stream lifecycle."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

UserEventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


class SpotUserDataStreamSupervisor:
    """Own listen-key renewal, socket reconnect and listen-key rotation.

    Binance listen keys expire unless renewed. A failed keepalive cancels the
    socket bound to that key, closes it best-effort and obtains a fresh key.
    The caller remains responsible for reconciling exchange state after every
    reconnect; stream events are hints, REST order/trade queries are authority.
    """

    def __init__(
        self,
        *,
        adapter,
        websocket,
        on_event: UserEventHandler,
        on_connect: Callable[[], Awaitable[None] | None] | None = None,
        keepalive_seconds: float = 25 * 60,
        reconnect_seconds: float = 2.0,
        max_reconnect_seconds: float = 60.0,
    ) -> None:
        if keepalive_seconds <= 0 or reconnect_seconds <= 0:
            raise ValueError("stream timing values must be positive")
        self.adapter = adapter
        self.websocket = websocket
        self.on_event = on_event
        self.on_connect = on_connect
        self.keepalive_seconds = keepalive_seconds
        self.reconnect_seconds = reconnect_seconds
        self.max_reconnect_seconds = max_reconnect_seconds
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._last_error: str | None = None
        self._restarts = 0

    @property
    def health(self) -> dict[str, Any]:
        return {
            "running": self._task is not None and not self._task.done(),
            "restarts": self._restarts,
            "last_error": self._last_error,
        }

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("Spot User Data Stream is already running")
        self._stopping = False
        self._task = asyncio.create_task(
            self._run(), name="aitos-spot-user-data-stream"
        )

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _consume(self, listen_key: str) -> None:
        async for event in self.websocket.user_stream(listen_key, reconnect=False):
            if not isinstance(event, dict):
                continue
            result = self.on_event(event)
            if inspect.isawaitable(result):
                await result

    async def _keepalive(self, listen_key: str) -> None:
        while True:
            await asyncio.sleep(self.keepalive_seconds)
            await self.adapter.keepalive_listen_key(listen_key)

    async def _close_key(self, listen_key: str) -> None:
        try:
            await self.adapter.close_listen_key(listen_key)
        except Exception:
            pass

    async def _run(self) -> None:
        delay = self.reconnect_seconds
        while not self._stopping:
            listen_key = None
            consumer = keeper = None
            try:
                listen_key = await self.adapter.create_listen_key()
                if self.on_connect is not None:
                    result = self.on_connect()
                    if inspect.isawaitable(result):
                        await result
                consumer = asyncio.create_task(self._consume(listen_key))
                keeper = asyncio.create_task(self._keepalive(listen_key))
                done, _ = await asyncio.wait(
                    {consumer, keeper}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    error = task.exception()
                    if error is not None:
                        raise error
                if not self._stopping:
                    raise RuntimeError("Spot User Data Stream ended unexpectedly")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)[:500]
                self._restarts += 1
            finally:
                for task in (consumer, keeper):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(
                    *(task for task in (consumer, keeper) if task is not None),
                    return_exceptions=True,
                )
                if listen_key is not None:
                    await self._close_key(listen_key)
            if not self._stopping:
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.max_reconnect_seconds)
