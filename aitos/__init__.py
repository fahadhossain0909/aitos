"""AITOS — AI Trading Operating System.

Foundation layer: core contracts, event bus, AI kernel, agent framework.
See docs/ARCHITECTURE.md and the AITOS Skill Specification for full context.
"""

from __future__ import annotations

import asyncio
from typing import Any

__version__ = "0.1.0"


if not hasattr(asyncio, "timeout"):

    class _CompatTimeout:
        """Minimal asyncio.timeout-compatible context manager for Python 3.10."""

        def __init__(self, delay: float) -> None:
            self._delay = delay
            self._task: asyncio.Task[Any] | None = None
            self._handle: asyncio.TimerHandle | None = None
            self._expired = False

        async def __aenter__(self) -> _CompatTimeout:
            loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            self._handle = loop.call_later(self._delay, self._cancel_task)
            return self

        def _cancel_task(self) -> None:
            self._expired = True
            if self._task is not None:
                self._task.cancel()

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            if self._handle is not None:
                self._handle.cancel()
            if self._expired and exc_type is asyncio.CancelledError:
                raise TimeoutError
            return False

    asyncio.timeout = _CompatTimeout  # type: ignore[attr-defined]
