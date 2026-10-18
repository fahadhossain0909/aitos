"""Cross-platform signal handling for AITOS entrypoints.

``loop.add_signal_handler`` is not implemented on Windows event loops
(ProactorEventLoop) and raises ``NotImplementedError``. Entrypoints must
still start there — local Windows validation runs paper trading natively —
so fall back to the portable ``signal.signal`` API when the loop-level API
is unavailable.
"""

from __future__ import annotations

import asyncio
import signal


def install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Set ``stop_event`` on SIGINT/SIGTERM, on every supported platform."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows: signal handlers require the main thread, which is
            # where every AITOS entrypoint runs its event loop.
            signal.signal(sig, lambda *_: stop_event.set())
