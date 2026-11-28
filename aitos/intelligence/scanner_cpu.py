"""Bounded process-isolated CPU work for scanner feature extraction.

Only deterministic, side-effect-free feature builders live here. Network I/O,
scanner state mutation, funnel writes and trade/risk decisions stay in the
parent event loop. A single spawned worker avoids GIL contention without
forking an already-running async process.
"""

from __future__ import annotations

import atexit
import os
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from typing import Any

_EXECUTOR: ProcessPoolExecutor | None = None


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
        try:
            workers = int(os.getenv("AITOS_SCANNER_CPU_WORKERS", "1"))
        except ValueError:
            workers = 1
        workers = max(1, min(2, workers))
        _EXECUTOR = ProcessPoolExecutor(
            max_workers=workers,
            mp_context=get_context("spawn"),
        )
    return _EXECUTOR


def shutdown_scanner_cpu_executor() -> None:
    """Release the shared worker during normal process shutdown."""
    global _EXECUTOR
    executor, _EXECUTOR = _EXECUTOR, None
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)


atexit.register(shutdown_scanner_cpu_executor)
