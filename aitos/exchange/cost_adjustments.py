"""Safe publication of Binance account-income records into trade lifecycle."""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.logging_setup import get_logger

logger = get_logger("aitos.exchange.cost_adjustments")

TradeResolver = Callable[[dict[str, Any]], Awaitable[str | None] | str | None]


class UnattributedCostAdjustment(RuntimeError):
    """Raised when an exchange cost cannot be mapped to one internal trade."""


class BinanceCostAdjustmentPublisher:
    """Translate account-income rows to idempotent lifecycle cost events.

    The resolver is mandatory because Binance funding rows may not carry a
    fill/order identity. The publisher deliberately refuses to guess which
    internal position owns a fee or funding payment.
    """

    def __init__(self, event_bus: Any, resolve_trade_id: TradeResolver) -> None:
        self._event_bus = event_bus
        self._resolve_trade_id = resolve_trade_id

    async def publish_income_rows(self, rows: Iterable[dict[str, Any]]) -> int:
        published = 0
        for row in rows:
            income_type = str(row.get("incomeType", "")).upper()
            if income_type not in {"COMMISSION", "FUNDING_FEE"}:
                continue
            if str(row.get("asset", "")).upper() != "USDT":
                raise ValueError("only USDT income can be booked as USD adjustments")
            try:
                amount = float(row["income"])
                transaction_id = str(row["tranId"]).strip()
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "income row is missing a valid amount or tranId"
                ) from exc
            if not math.isfinite(amount) or not transaction_id:
                raise ValueError("income amount and tranId must be finite/non-empty")

            resolved_trade_id = self._resolve_trade_id(row)
            trade_id = (
                await resolved_trade_id
                if inspect.isawaitable(resolved_trade_id)
                else resolved_trade_id
            )
            if not trade_id:
                raise UnattributedCostAdjustment(
                    f"cannot safely attribute Binance income transaction {transaction_id}"
                )

            kind = "fee" if income_type == "COMMISSION" else "funding"
            # Binance reports commission as a negative income; lifecycle fee
            # accounting expects a positive cost. Funding remains signed.
            amount_usd = abs(amount) if kind == "fee" else amount
            await self._event_bus.publish(
                Event(
                    topic="trade.cost_adjustment",
                    payload={
                        "trade_id": str(trade_id),
                        "adjustment_id": f"binance:income:{transaction_id}",
                        "kind": kind,
                        "amount_usd": amount_usd,
                        "exchange": "binance_usdm",
                        "exchange_transaction_id": transaction_id,
                        "income_type": income_type,
                        "symbol": str(row.get("symbol", "")).upper(),
                        "asset": "USDT",
                        "exchange_time_ms": int(row.get("time", 0)),
                    },
                    source_module="binance-cost-adjustment-reconciler",
                )
            )
            published += 1
        return published


class BinanceIncomeReconciliationService(AITOSModule):
    """Periodically reconcile Binance fees/funding against uniquely matched trades.

    Attribution is deliberately conservative: a row is applied only when its
    symbol and exchange timestamp identify exactly one internal trade. Ambiguous
    rows hold the durable cursor before that row so they are retried rather than
    silently skipped or booked against a guessed position.
    """

    def __init__(
        self,
        exchange: Any,
        lifecycle: Any,
        event_bus: Any,
        state_store: Any,
        *,
        interval_seconds: float = 300.0,
        overlap_ms: int = 60_000,
        commission_close_grace_ms: int = 120_000,
        account: str = "binance_usdm",
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if overlap_ms < 0 or commission_close_grace_ms < 0:
            raise ValueError("income reconciliation windows must be non-negative")
        self._exchange = exchange
        self._lifecycle = lifecycle
        self._event_bus = event_bus
        self._state_store = state_store
        self._interval_seconds = float(interval_seconds)
        self._overlap_ms = int(overlap_ms)
        self._commission_close_grace_ms = int(commission_close_grace_ms)
        self._account = account
        self._publisher = BinanceCostAdjustmentPublisher(
            event_bus, self.resolve_trade_id
        )
        self._task: asyncio.Task | None = None
        self._initialized = False
        self._errors = 0
        self._runs = 0
        self._published = 0
        self._unattributed = 0
        self._last_run_at: str | None = None
        self._last_error: str | None = None

    @property
    def module_id(self) -> str:
        return "binance-income-reconciliation"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: dict[str, Any]) -> None:
        if self._initialized:
            return
        self._task = asyncio.create_task(
            self._run_loop(), name="binance-income-reconciliation"
        )
        self._initialized = True

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=grace_period_seconds)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self._initialized = False

    async def health_check(self) -> HealthStatus:
        task_alive = self._task is not None and not self._task.done()
        status = ModuleStatus.HEALTHY if task_alive else ModuleStatus.UNHEALTHY
        if self._last_error is not None and task_alive:
            status = ModuleStatus.DEGRADED
        return HealthStatus(
            module_id=self.module_id,
            status=status,
            latency_ms=0.0,
            last_event_time=self._last_run_at,
            details={
                "runs": self._runs,
                "published_adjustments": self._published,
                "unattributed_rows": self._unattributed,
                "errors": self._errors,
                "last_error": self._last_error,
                "account": self._account,
            },
        )

    async def emit_events(self) -> AsyncIterator[Event]:
        if False:
            yield Event(topic="internal.noop", payload={}, source_module=self.module_id)

    async def handle_event(self, event: Event) -> EventResponse | None:
        return None

    def _trades(self) -> list[Any]:
        by_id: dict[str, Any] = {}
        for trade in (
            *self._lifecycle.get_open_trades(),
            *self._lifecycle.get_closed_trades(),
        ):
            trade_id = str(getattr(trade, "trade_id", "") or "")
            if trade_id:
                by_id[trade_id] = trade
        return list(by_id.values())

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def resolve_trade_id(self, row: dict[str, Any]) -> str | None:
        symbol = str(row.get("symbol", "")).strip().upper()
        if not symbol:
            return None
        try:
            event_time = datetime.fromtimestamp(
                int(row.get("time")) / 1000.0, tz=timezone.utc
            )
        except (TypeError, ValueError, OSError, OverflowError):
            return None

        income_type = str(row.get("incomeType", "")).upper()
        grace_ms = self._commission_close_grace_ms if income_type == "COMMISSION" else 0
        matches: list[str] = []
        for trade in self._trades():
            if str(getattr(trade, "symbol", "")).upper() != symbol:
                continue
            entry_time = self._parse_time(getattr(trade, "entry_time", None))
            if entry_time is None or event_time < entry_time:
                continue
            exit_time_value = getattr(trade, "exit_time", None)
            if exit_time_value:
                exit_time = self._parse_time(exit_time_value)
                if exit_time is None:
                    continue
                if (event_time - exit_time).total_seconds() * 1000 > grace_ms:
                    continue
            elif getattr(getattr(trade, "state", None), "value", None) not in {
                "position_opened",
                "exit_triggered",
            }:
                continue
            matches.append(str(trade.trade_id))

        unique_matches = list(dict.fromkeys(matches))
        return unique_matches[0] if len(unique_matches) == 1 else None

    async def run_once(self) -> dict[str, int]:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        cursor = await self._state_store.load_income_cursor(self._account)
        trades = self._trades()
        if cursor is None:
            entry_times = [
                self._parse_time(getattr(trade, "entry_time", None)) for trade in trades
            ]
            valid_entry_times = [value for value in entry_times if value is not None]
            if not valid_entry_times:
                await self._state_store.save_income_cursor(now_ms, self._account)
                self._runs += 1
                self._last_run_at = datetime.now(timezone.utc).isoformat()
                return {"rows": 0, "published": 0, "unattributed": 0}
            start_time_ms = int(min(valid_entry_times).timestamp() * 1000)
        else:
            start_time_ms = max(0, int(cursor) - self._overlap_ms)

        rows = await self._exchange.fetch_income_history(
            start_time_ms=start_time_ms,
            end_time_ms=now_ms,
            limit=1000,
        )
        rows = sorted(
            rows,
            key=lambda row: (
                int(row.get("time", 0)),
                str(row.get("tranId", "")),
            ),
        )
        published = 0
        unresolved_times: list[int] = []
        for row in rows:
            try:
                published += await self._publisher.publish_income_rows([row])
            except UnattributedCostAdjustment:
                self._unattributed += 1
                try:
                    unresolved_times.append(int(row.get("time", 0)))
                except (TypeError, ValueError):
                    unresolved_times.append(start_time_ms)
                logger.error(
                    "Binance income row could not be uniquely attributed; cursor held",
                    extra={
                        "aitos_extra": {
                            "tran_id": row.get("tranId"),
                            "symbol": row.get("symbol"),
                            "income_type": row.get("incomeType"),
                            "time": row.get("time"),
                        }
                    },
                )

        next_cursor = max(0, min(unresolved_times) - 1) if unresolved_times else now_ms
        await self._state_store.save_income_cursor(next_cursor, self._account)
        self._runs += 1
        self._published += published
        self._last_run_at = datetime.now(timezone.utc).isoformat()
        return {
            "rows": len(rows),
            "published": published,
            "unattributed": len(unresolved_times),
        }

    async def _run_loop(self) -> None:
        try:
            while True:
                try:
                    await self.run_once()
                    self._last_error = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._errors += 1
                    self._last_error = str(exc)
                    logger.exception("Binance income reconciliation failed")
                await asyncio.sleep(self._interval_seconds)
        except asyncio.CancelledError:
            return
