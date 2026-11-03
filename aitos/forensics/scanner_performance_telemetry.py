"""Observational telemetry for scanner compute attribution.

This module measures scanner wall-clock time and CPU throttling without changing
scanner decisions, feature weights, thresholds, or symbol selection.
"""

from __future__ import annotations

import time
from functools import wraps
from typing import Any

_INSTALLED = False


def _logger():
    from aitos.logging_setup import get_logger

    return get_logger("aitos.forensics.scanner")


def _cgroup_stats() -> dict[str, int] | None:
    for path in ("/sys/fs/cgroup/cpu.stat", "/sys/fs/cgroup/cpu/cpu.stat"):
        try:
            result: dict[str, int] = {}
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    parts = line.split()
                    if len(parts) == 2:
                        try:
                            result[parts[0]] = int(parts[1])
                        except ValueError:
                            continue
            if result:
                return result
        except OSError:
            continue
    return None


def install() -> None:
    """Install timing wrappers once; instrumentation is strictly observational."""
    global _INSTALLED
    if _INSTALLED:
        return
    try:
        from aitos.intelligence.scanner import OpportunityScanner
    except Exception:
        return

    _INSTALLED = True
    original_scan_symbol = OpportunityScanner.scan_symbol
    original_scan_all = OpportunityScanner.scan_all

    @wraps(original_scan_symbol)
    async def scan_symbol(self: Any, symbol: str, *args: Any, **kwargs: Any):
        started = time.perf_counter()
        cpu_started = time.process_time()
        before_cpu = _cgroup_stats() or {}
        result = None
        error = None
        try:
            result = await original_scan_symbol(self, symbol, *args, **kwargs)
            return result
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            wall_ms = (time.perf_counter() - started) * 1000
            process_cpu_ms = (time.process_time() - cpu_started) * 1000
            after_cpu = _cgroup_stats() or {}
            throttled_usec = max(
                0,
                after_cpu.get("throttled_usec", 0)
                - before_cpu.get("throttled_usec", 0),
            )
            throttled_count = max(
                0,
                after_cpu.get("nr_throttled", 0) - before_cpu.get("nr_throttled", 0),
            )
            logger = _logger()
            logger.info(
                "scanner symbol performance",
                extra={
                    "aitos_extra": {
                        "symbol": symbol,
                        "wall_ms": round(wall_ms, 3),
                        "process_cpu_ms": round(process_cpu_ms, 3),
                        "cpu_throttled_ms": round(throttled_usec / 1000.0, 3),
                        "cpu_throttle_events": throttled_count,
                        "candidate": result is not None,
                        "score": (
                            round(float(result.composite_score), 3)
                            if result is not None
                            else None
                        ),
                        "error": error,
                    },
                },
            )

    @wraps(original_scan_all)
    async def scan_all(self: Any, *args: Any, **kwargs: Any):
        started = time.perf_counter()
        cpu_started = time.process_time()
        before_cpu = _cgroup_stats() or {}
        result = await original_scan_all(self, *args, **kwargs)
        after_cpu = _cgroup_stats() or {}
        wall_ms = (time.perf_counter() - started) * 1000
        process_cpu_ms = (time.process_time() - cpu_started) * 1000
        logger = _logger()
        logger.info(
            "scanner cycle performance",
            extra={
                "aitos_extra": {
                    "symbols": list(self._symbols),
                    "symbol_count": len(self._symbols),
                    "wall_ms": round(wall_ms, 3),
                    "process_cpu_ms": round(process_cpu_ms, 3),
                    "cpu_throttled_ms": round(
                        max(
                            0,
                            after_cpu.get("throttled_usec", 0)
                            - before_cpu.get("throttled_usec", 0),
                        )
                        / 1000.0,
                        3,
                    ),
                    "cpu_throttle_events": max(
                        0,
                        after_cpu.get("nr_throttled", 0)
                        - before_cpu.get("nr_throttled", 0),
                    ),
                    "candidates": len(result),
                },
            },
        )
        return result

    OpportunityScanner.scan_symbol = scan_symbol
    OpportunityScanner.scan_all = scan_all
