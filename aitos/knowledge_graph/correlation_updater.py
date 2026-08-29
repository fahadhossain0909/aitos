"""SymbolCorrelationUpdater — periodically computes real pairwise return
correlations across the tracked symbol universe (reusing
``indicators.pearson_correlation``, the same function the Opportunity
Scanner's lead-lag scoring uses) and pushes the results into the
Knowledge Graph as ``CORRELATED_WITH`` edges.

Same background-loop pattern as ``ReconciliationScheduler``: an interval,
a ``run_once`` for both the loop and manual/startup invocation, and
per-pair error isolation so one bad symbol doesn't block the rest.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from itertools import combinations
from typing import Any

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.core.exceptions import ModuleNotInitializedError
from aitos.exchange.base import ExchangeAdapter
from aitos.intelligence.indicators import pearson_correlation, returns
from aitos.knowledge_graph.writer import KnowledgeGraphWriter
from aitos.logging_setup import get_logger
from aitos.models.trade import utc_now_iso

logger = get_logger("aitos.knowledge_graph.correlation_updater")

DEFAULT_INTERVAL_SECONDS = 3600.0  # correlations drift slowly; hourly is plenty


class SymbolCorrelationUpdater(AITOSModule):
    def __init__(
        self,
        exchange: ExchangeAdapter,
        graph_writer: KnowledgeGraphWriter,
        symbols: list[str],
        timeframe: str = "1h",
        kline_lookback: int = 100,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._exchange = exchange
        self._graph_writer = graph_writer
        self._symbols = symbols
        self._timeframe = timeframe
        self._kline_lookback = kline_lookback
        self._interval_seconds = interval_seconds
        self._initialized = False
        self._task: asyncio.Task | None = None
        self._last_run_at: str | None = None
        self._pairs_updated_last_run = 0
        self._errors = 0

    # -- AITOSModule contract -------------------------------------------------

    @property
    def module_id(self) -> str:
        return "symbol-correlation-updater"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: dict[str, Any]) -> None:
        if self._initialized:
            return
        self._task = asyncio.create_task(
            self._run_loop(), name="correlation-update-loop"
        )
        self._initialized = True
        logger.info(
            "SymbolCorrelationUpdater initialized",
            extra={"aitos_extra": {"symbols": self._symbols}},
        )

    async def health_check(self) -> HealthStatus:
        task_alive = self._task is not None and not self._task.done()
        return HealthStatus(
            module_id=self.module_id,
            status=ModuleStatus.HEALTHY if task_alive else ModuleStatus.UNHEALTHY,
            latency_ms=0.0,
            last_event_time=self._last_run_at,
            details={
                "pairs_updated_last_run": self._pairs_updated_last_run,
                "errors": self._errors,
            },
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=grace_period_seconds)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        logger.info("SymbolCorrelationUpdater shut down")

    async def emit_events(self) -> AsyncIterator[Event]:
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Event) -> EventResponse | None:
        return None

    # -- Public API ---------------------------------------------------------------

    async def run_once(self) -> int:
        """Fetch fresh klines for every tracked symbol and update every
        pairwise correlation edge. Returns how many pairs were updated."""
        self._require_initialized()
        klines_by_symbol = {}
        for symbol in self._symbols:
            try:
                klines_by_symbol[symbol] = await self._exchange.fetch_klines(
                    symbol, self._timeframe, limit=self._kline_lookback
                )
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                logger.error(
                    "failed to fetch klines for correlation update",
                    extra={"aitos_extra": {"symbol": symbol, "error": str(exc)}},
                )

        updated = 0
        now = utc_now_iso()
        for symbol_a, symbol_b in combinations(klines_by_symbol.keys(), 2):
            try:
                returns_a = returns(klines_by_symbol[symbol_a])
                returns_b = returns(klines_by_symbol[symbol_b])
                coefficient = pearson_correlation(returns_a, returns_b)
                await self._graph_writer.update_symbol_correlation(
                    symbol_a, symbol_b, coefficient, now
                )
                updated += 1
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                logger.error(
                    "failed to update correlation edge",
                    extra={
                        "aitos_extra": {"pair": (symbol_a, symbol_b), "error": str(exc)}
                    },
                )

        self._last_run_at = now
        self._pairs_updated_last_run = updated
        return updated

    # -- Internals --------------------------------------------------------------

    async def _run_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval_seconds)
                try:
                    await self.run_once()
                except Exception as exc:  # noqa: BLE001
                    self._errors += 1
                    logger.error("correlation update loop iteration failed: %s", exc)
        except asyncio.CancelledError:
            return

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError(
                "SymbolCorrelationUpdater.initialize() must be called first"
            )
