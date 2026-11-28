"""Bounded process-isolated CPU work for scanner feature extraction.

Only deterministic, side-effect-free feature builders live here. Network I/O,
scanner state mutation, funnel writes and trade/risk decisions stay in the
parent event loop. A single spawned worker avoids GIL contention without
forking an already-running async process.
"""

from __future__ import annotations

import asyncio
import atexit
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from typing import Any

_EXECUTOR: ProcessPoolExecutor | None = None
_CPU_SLOT = asyncio.Semaphore(1)


def compute_amt_footprint_features(
    trades: list[Any],
    klines: list[Any],
    order_book: Any,
    previous_profile: Any,
    tick_size: float,
    value_area_pct: float,
    ib_minutes: int,
    imbalance_threshold: float,
    min_level_volume: float,
) -> tuple[Any, Any]:
    """Compute AMT and footprint features in an isolated CPU process."""
    from aitos.intelligence.amt.engine import AMTEngine
    from aitos.intelligence.footprint import FootprintEngine
    from aitos.intelligence.footprint_signals import FootprintSignalEngine

    amt_context = AMTEngine(
        tick_size, value_area_pct=value_area_pct, ib_minutes=ib_minutes
    ).analyze(
        trades,
        klines=klines,
        book=order_book,
        previous_profile=previous_profile,
    )
    footprint = FootprintEngine(tick_size).build(trades)
    signals = FootprintSignalEngine(
        imbalance_threshold=imbalance_threshold,
        min_level_volume=min_level_volume,
    ).evaluate(footprint)
    return amt_context, signals


def get_scanner_cpu_executor() -> ProcessPoolExecutor:
    """Return the process-wide, bounded scanner CPU executor."""
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ProcessPoolExecutor(
            max_workers=1,
            mp_context=get_context("spawn"),
        )
    return _EXECUTOR


async def run_scanner_cpu(function, *args):
    """Submit at most one CPU task and retain the slot after caller cancellation."""
    await _CPU_SLOT.acquire()
    loop = asyncio.get_running_loop()
    try:
        future = loop.run_in_executor(get_scanner_cpu_executor(), function, *args)
    except BaseException:
        _CPU_SLOT.release()
        raise

    release_on_completion = False
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        release_on_completion = True

        def release_slot(done) -> None:
            try:
                done.exception()
            except asyncio.CancelledError:
                pass
            _CPU_SLOT.release()

        future.add_done_callback(release_slot)
        raise
    finally:
        if not release_on_completion:
            _CPU_SLOT.release()


def shutdown_scanner_cpu_executor() -> None:
    """Release the shared worker during normal process shutdown."""
    global _EXECUTOR
    executor, _EXECUTOR = _EXECUTOR, None
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)


atexit.register(shutdown_scanner_cpu_executor)
