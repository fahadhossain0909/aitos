"""Regression tests for cross-platform entrypoint signal handling.

On Windows the Proactor event loop raises ``NotImplementedError`` from
``loop.add_signal_handler``, which previously crashed ``run_paper_trading.py``
and ``run_live_trading.py`` at startup (verified on this host). The helper
must fall back to the portable ``signal.signal`` API instead.
"""

from __future__ import annotations

import asyncio

from aitos.runtime_signals import install_signal_handlers


def test_install_signal_handlers_survives_proactor_loop() -> None:
    """Must not raise NotImplementedError on Windows ProactorEventLoop."""

    async def scenario() -> asyncio.Event:
        stop_event = asyncio.Event()
        install_signal_handlers(stop_event)
        return stop_event

    if hasattr(asyncio, "ProactorEventLoop"):
        loop = asyncio.ProactorEventLoop()
        try:
            loop.run_until_complete(scenario())
        finally:
            loop.close()
    else:
        asyncio.run(scenario())
