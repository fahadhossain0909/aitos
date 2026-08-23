"""ReconciliationScheduler — closes the gap flagged at the end of the
exchange-side-stops phase: `TradeLifecycle.reconcile_trade` existed, but
nothing called it automatically. This module does, on a fixed interval,
for every currently open trade.

This is the actual resilience payoff of exchange-side stops: if this
process was down when a stop or take-profit filled on the exchange, the
first reconciliation pass after startup catches it and closes the trade
correctly instead of leaving a stale `POSITION_OPENED` record forever.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Optional

from aitos.core.contracts import (AITOSModule, Event, EventResponse,
                                  HealthStatus, ModuleStatus)
from aitos.core.exceptions import ModuleNotInitializedError
from aitos.eventbus.redis_bus import EventBus
from aitos.logging_setup import get_logger
from aitos.models.trade import TradeLifecycleState
from aitos.trading.lifecycle import TradeLifecycle

logger = get_logger("aitos.trading.reconciliation")

TOPIC_RECONCILIATION_RUN = "trade.reconciliation_run"

DEFAULT_INTERVAL_SECONDS = 30.0


class ReconciliationScheduler(AITOSModule):
    def __init__(
        self,
        trade_lifecycle: TradeLifecycle,
        event_bus: EventBus,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._lifecycle = trade_lifecycle
        self._event_bus = event_bus
        self._interval_seconds = interval_seconds
        self._initialized = False
        self._task: Optional[asyncio.Task] = None
        self._last_run_at: Optional[str] = None
        self._last_run_trades_checked = 0
        self._last_run_trades_closed = 0
        self._total_runs = 0
        self._errors = 0

    # -- AITOSModule contract -------------------------------------------------

    @property
    def module_id(self) -> str:
        return "reconciliation-scheduler"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: Dict[str, Any]) -> None:
        if self._initialized:
            return
        self._task = asyncio.create_task(self._run_loop(), name="reconciliation-loop")
        self._initialized = True
        logger.info(
            "ReconciliationScheduler initialized",
            extra={"aitos_extra": {"interval_seconds": self._interval_seconds}},
        )

    async def health_check(self) -> HealthStatus:
        task_alive = self._task is not None and not self._task.done()
        status = ModuleStatus.HEALTHY if task_alive else ModuleStatus.UNHEALTHY
        if self._errors > 0 and task_alive:
            status = ModuleStatus.DEGRADED
        return HealthStatus(
            module_id=self.module_id,
            status=status,
            latency_ms=0.0,
            last_event_time=self._last_run_at,
            details={
                "total_runs": self._total_runs,
                "errors": self._errors,
                "last_run_trades_checked": self._last_run_trades_checked,
                "last_run_trades_closed": self._last_run_trades_closed,
            },
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=grace_period_seconds)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        logger.info("ReconciliationScheduler shut down")

    async def emit_events(self) -> AsyncIterator[Event]:
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Event) -> Optional[EventResponse]:
        return None

    # -- Public API ---------------------------------------------------------------

    async def run_once(self) -> int:
        """Reconcile every currently open trade immediately (rather than
        waiting for the next scheduled tick) — call this right after
        startup or reconnecting to the exchange, in addition to the
        background loop. Returns how many trades were closed as a result."""
        self._require_initialized()
        open_trades = self._lifecycle.get_open_trades()
        closed_count = 0

        for trade in open_trades:
            try:
                result = await self._lifecycle.reconcile_trade(trade.trade_id)
                if result.state == TradeLifecycleState.POSITION_CLOSED:
                    closed_count += 1
                    logger.warning(
                        "reconciliation closed a trade the live loop had missed",
                        extra={
                            "aitos_extra": {
                                "trade_id": trade.trade_id,
                                "exit_reason": result.exit_reason,
                            }
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                logger.error(
                    "reconciliation failed for trade",
                    extra={
                        "aitos_extra": {"trade_id": trade.trade_id, "error": str(exc)}
                    },
                )

        self._total_runs += 1
        self._last_run_at = datetime.now(timezone.utc).isoformat()
        self._last_run_trades_checked = len(open_trades)
        self._last_run_trades_closed = closed_count

        await self._event_bus.publish(
            Event(
                topic=TOPIC_RECONCILIATION_RUN,
                payload={
                    "trades_checked": len(open_trades),
                    "trades_closed": closed_count,
                },
                source_module=self.module_id,
            )
        )
        return closed_count

    # -- Internals --------------------------------------------------------------

    async def _run_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval_seconds)
                try:
                    await self.run_once()
                except Exception as exc:  # noqa: BLE001
                    self._errors += 1
                    logger.error("reconciliation loop iteration failed: %s", exc)
        except asyncio.CancelledError:
            return

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError(
                "ReconciliationScheduler.initialize() must be called first"
            )
