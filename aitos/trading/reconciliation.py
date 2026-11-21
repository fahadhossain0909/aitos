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
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.core.exceptions import ModuleNotInitializedError
from aitos.eventbus.redis_bus import EventBus
from aitos.logging_setup import get_logger
from aitos.models.trade import TradeLifecycleState
from aitos.execution.protection_reconciliation import (
    recover_triggered_protection_child_fills,
)
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
        self._task: asyncio.Task | None = None
        self._last_run_at: str | None = None
        self._last_run_trades_checked = 0
        self._last_run_trades_closed = 0
        self._total_runs = 0
        self._errors = 0
        self._run_lock = asyncio.Lock()
        self._last_child_fills_applied = 0
        self._last_child_fills_blocked = 0
        self._last_child_recovery_monotonic = 0.0
        self._child_recovery_interval_seconds = min(max(interval_seconds, 5.0), 30.0)

    # -- AITOSModule contract -------------------------------------------------

    @property
    def module_id(self) -> str:
        return "reconciliation-scheduler"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: dict[str, Any]) -> None:
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
                "last_child_fills_applied": self._last_child_fills_applied,
                "last_child_fills_blocked": self._last_child_fills_blocked,
                "account_reconciliation_error": bool(
                    getattr(self._lifecycle, "_account_reconciliation_error", False)
                ),
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

    async def handle_event(self, event: Event) -> EventResponse | None:
        return None

    # -- Public API ---------------------------------------------------------------

    async def run_once(self) -> int:
        """Reconcile protective child fills before ordinary order-status exits.

        This method is shared by the startup caller and the periodic scheduler.
        Serializing it prevents overlapping passes from applying the same child
        fill while a previous pass is still persisting the trade snapshot.
        """
        self._require_initialized()
        async with self._run_lock:
            return await self._run_once_locked()

    async def _run_once_locked(self) -> int:
        open_trades = self._lifecycle.get_open_trades()
        closed_count = 0
        handled_trade_ids: set[str] = set()
        child_applied = 0
        child_blocked = 0

        executor = getattr(self._lifecycle, "_order_executor", None)
        if getattr(executor, "requires_exchange_execution", False):

            def has_algo_protection(trade: Any) -> bool:
                stop_id = getattr(trade, "sl_order_id", None)
                tp_ids = getattr(trade, "tp_order_ids", ())
                return (isinstance(stop_id, str) and stop_id.startswith("algo:")) or (
                    isinstance(tp_ids, (list, tuple))
                    and any(
                        isinstance(order_id, str) and order_id.startswith("algo:")
                        for order_id in tp_ids
                    )
                )

            algo_protected_trade_ids = {
                str(trade.trade_id)
                for trade in open_trades
                if has_algo_protection(trade)
            }
            # The generic reconcile_trade path is not authoritative for an
            # algo child fill: it can issue a second market reduction. Every
            # algo-protected trade stays out of that path, even between
            # throttled authenticated child-inventory checks.
            handled_trade_ids.update(algo_protected_trade_ids)
            now_monotonic = time.monotonic()
            child_recovery_due = (
                now_monotonic - self._last_child_recovery_monotonic
                >= self._child_recovery_interval_seconds
            )
            journal = getattr(executor, "execution_intent_store", None)
            if child_recovery_due:
                self._last_child_recovery_monotonic = now_monotonic
            if not child_recovery_due:
                journal = None
            if child_recovery_due and journal is None:
                child_blocked = len(algo_protected_trade_ids) or 1
                self._lifecycle._account_reconciliation_error = True
                self._errors += 1
                logger.critical(
                    "background child-fill recovery has no durable execution journal",
                    extra={"aitos_extra": {"new_entries_allowed": False}},
                )
            elif child_recovery_due:
                try:
                    child_report = await recover_triggered_protection_child_fills(
                        executor, journal, self._lifecycle
                    )
                except Exception as exc:  # noqa: BLE001
                    child_blocked = len(algo_protected_trade_ids) or 1
                    self._lifecycle._account_reconciliation_error = True
                    self._errors += 1
                    logger.critical(
                        "background protective child recovery failed closed",
                        extra={
                            "aitos_extra": {
                                "error": f"{type(exc).__name__}: {exc}",
                                "new_entries_allowed": False,
                            }
                        },
                    )
                else:
                    applied = (
                        child_report.get("applied", [])
                        if isinstance(child_report, dict)
                        else None
                    )
                    blocked = (
                        child_report.get("blocked", [])
                        if isinstance(child_report, dict)
                        else None
                    )
                    if not isinstance(applied, list) or not isinstance(blocked, list):
                        child_blocked = len(algo_protected_trade_ids) or 1
                        self._lifecycle._account_reconciliation_error = True
                        self._errors += 1
                    else:
                        child_applied = len(applied)
                        child_blocked = len(blocked)
                        for outcome in applied:
                            if not isinstance(outcome, dict):
                                handled_trade_ids.update(algo_protected_trade_ids)
                                continue
                            trade_id = outcome.get("trade_id")
                            if isinstance(trade_id, str) and trade_id:
                                handled_trade_ids.add(trade_id)
                            else:
                                handled_trade_ids.update(
                                    str(t.trade_id) for t in open_trades
                                )
                                child_blocked += 1
                        for finding in blocked:
                            if not isinstance(finding, dict):
                                handled_trade_ids.update(
                                    str(t.trade_id) for t in open_trades
                                )
                                continue
                            trade_id = finding.get("trade_id")
                            if isinstance(trade_id, str) and trade_id:
                                handled_trade_ids.add(trade_id)
                            else:
                                handled_trade_ids.update(
                                    str(t.trade_id) for t in open_trades
                                )
                        nonterminal = any(
                            not isinstance(outcome, dict)
                            or outcome.get("terminal") is not True
                            for outcome in applied
                        )
                        if nonterminal:
                            child_blocked += sum(
                                1
                                for outcome in applied
                                if not isinstance(outcome, dict)
                                or outcome.get("terminal") is not True
                            )
                        if blocked or nonterminal or child_blocked:
                            self._lifecycle._account_reconciliation_error = True
                            self._errors += 1

        for trade in open_trades:
            trade_id = str(trade.trade_id)
            if trade_id in handled_trade_ids:
                continue
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
        self._last_child_fills_applied = child_applied
        self._last_child_fills_blocked = child_blocked

        await self._event_bus.publish(
            Event(
                topic=TOPIC_RECONCILIATION_RUN,
                payload={
                    "trades_checked": len(open_trades),
                    "trades_closed": closed_count,
                    "protective_child_fills_applied": child_applied,
                    "protective_child_fills_blocked": child_blocked,
                    "handled_trade_ids": sorted(handled_trade_ids),
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
