"""Durable, fail-closed orchestration for switching one position to another.

The coordinator never assumes that an accepted order is a fill. The injected
close adapter must use a stable client order id, reconcile an existing order
before retrying, and confirm the venue position is flat before returning a
receipt. The durable journal prevents a restart from blindly repeating close
or open side effects.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from aitos.execution.order_executor import OrderExecutor, OrderRequest
from aitos.intelligence.capital_controls import opportunity_age_seconds
from aitos.intelligence.capital_gateway import CapitalGateway
from aitos.models.trade import Opportunity, Trade, TradeLifecycleState


@dataclass(frozen=True, slots=True)
class CloseReceipt:
    confirmed: bool
    order_id: str
    fill_price: float
    filled_quantity: float
    position_flat: bool


@dataclass(frozen=True, slots=True)
class SwitchIntent:
    switch_id: str
    incumbent_trade_id: str
    incumbent_symbol: str
    candidate_opportunity_id: str
    candidate_symbol: str
    state: str
    close_client_order_id: str
    close_order_id: str | None = None
    close_fill_price: float | None = None
    reason: str | None = None
    candidate_payload: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1


class SwitchJournal(Protocol):
    def get(self, switch_id: str) -> SwitchIntent | None: ...
    def create(self, intent: SwitchIntent) -> SwitchIntent: ...
    def update(self, switch_id: str, **changes: Any) -> SwitchIntent: ...
    def list_incomplete(self) -> list[SwitchIntent]: ...


class SQLiteSwitchJournal:
    """Small durable journal; use a persistent volume, never /tmp in production."""

    SQLITE_BUSY_TIMEOUT_MS = 30_000

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # Multiple worker processes may construct the journal simultaneously.
        # A bounded busy timeout lets WAL/DDL setup and writes serialize at the
        # database boundary instead of leaking SQLITE_BUSY to callers.
        with self._connect() as db:
            self._enable_wal_with_retry(db)
            self._execute_with_busy_retry(db, "PRAGMA synchronous=NORMAL")
            self._execute_with_busy_retry(
                db,
                """CREATE TABLE IF NOT EXISTS position_switch_intents (
                    switch_id TEXT PRIMARY KEY,
                    incumbent_trade_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )""",
            )
            self._execute_with_busy_retry(
                db,
                """CREATE UNIQUE INDEX IF NOT EXISTS one_active_switch_per_incumbent
                ON position_switch_intents(incumbent_trade_id)
                WHERE state NOT IN ('completed', 'failed')""",
            )

    def _execute_with_busy_retry(self, db, statement, parameters=()):
        deadline = time.monotonic() + self.SQLITE_BUSY_TIMEOUT_MS / 1000
        delay = 0.01
        while True:
            try:
                return db.execute(statement, parameters)
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if not any(token in message for token in ("locked", "busy")):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.25)

    def _enable_wal_with_retry(self, db):
        deadline = time.monotonic() + self.SQLITE_BUSY_TIMEOUT_MS / 1000
        delay = 0.01
        while True:
            try:
                mode = db.execute("PRAGMA journal_mode").fetchone()
                if mode and str(mode[0]).lower() == "wal":
                    return
                db.execute("PRAGMA journal_mode=WAL").fetchone()
                return
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if not any(token in message for token in ("locked", "busy")):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.25)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(
            self.path,
            timeout=self.SQLITE_BUSY_TIMEOUT_MS / 1000.0,
            isolation_level="DEFERRED",
        )
        db.execute(f"PRAGMA busy_timeout={self.SQLITE_BUSY_TIMEOUT_MS}")
        return db

    @staticmethod
    def _decode(payload: str) -> SwitchIntent:
        return SwitchIntent(**json.loads(payload))

    def get(self, switch_id: str) -> SwitchIntent | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM position_switch_intents WHERE switch_id=?",
                (switch_id,),
            ).fetchone()
        return self._decode(row[0]) if row else None

    def create(self, intent: SwitchIntent) -> SwitchIntent:
        payload = json.dumps(asdict(intent), sort_keys=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO position_switch_intents(switch_id,incumbent_trade_id,state,payload) VALUES(?,?,?,?)",
                (intent.switch_id, intent.incumbent_trade_id, intent.state, payload),
            )
            row = db.execute(
                "SELECT payload FROM position_switch_intents WHERE switch_id=?",
                (intent.switch_id,),
            ).fetchone()
        return self._decode(row[0])

    def update(self, switch_id: str, **changes: Any) -> SwitchIntent:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM position_switch_intents WHERE switch_id=?",
                (switch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown switch intent: {switch_id}")
            payload = json.loads(row[0])
            payload.update(changes)
            db.execute(
                "UPDATE position_switch_intents SET payload=?, state=?, updated_at=CURRENT_TIMESTAMP WHERE switch_id=?",
                (json.dumps(payload, sort_keys=True), payload["state"], switch_id),
            )
        return SwitchIntent(**payload)

    def list_incomplete(self) -> list[SwitchIntent]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM position_switch_intents WHERE state NOT IN ('completed', 'failed')"
            ).fetchall()
        return [self._decode(row[0]) for row in rows]


class CloseAndConfirm(Protocol):
    async def __call__(
        self, trade: Trade, client_order_id: str, existing_order_id: str | None
    ) -> CloseReceipt:
        """Idempotently submit/reconcile reduce-only close and verify flat state."""
        ...


PortfolioProvider = Callable[[], Any | Awaitable[Any]]
PairEvaluator = Callable[[Trade, Opportunity], Any | Awaitable[Any]]
PositionFlatCheck = Callable[[str], bool | Awaitable[bool]]
ReferencePriceProvider = Callable[[str], float | Awaitable[float]]


class OrderExecutorCloseAdapter:
    """Reduce-only close adapter that requires FILLED plus venue-flat confirmation."""

    def __init__(
        self,
        executor: OrderExecutor,
        position_is_flat: PositionFlatCheck,
        reference_price_provider: ReferencePriceProvider,
    ) -> None:
        self.executor = executor
        self.position_is_flat = position_is_flat
        self.reference_price_provider = reference_price_provider

    async def __call__(
        self, trade: Trade, client_order_id: str, existing_order_id: str | None
    ) -> CloseReceipt:
        status = None
        if existing_order_id:
            status = await self.executor.get_order_status(
                trade.symbol, existing_order_id
            )
        if status is None:
            status = await self.executor.get_order_by_client_id(
                trade.symbol, client_order_id
            )
        if status is None:
            reference_price = self.reference_price_provider(trade.symbol)
            if hasattr(reference_price, "__await__"):
                reference_price = await reference_price
            reference_price = float(reference_price)
            if not math.isfinite(reference_price) or reference_price <= 0:
                raise ValueError("fresh positive reference price is required for close")
            result = await self.executor.submit_order(
                OrderRequest(
                    symbol=trade.symbol,
                    side=trade.side,
                    quantity=trade.quantity,
                    reference_price=reference_price,
                    client_order_id=client_order_id,
                    reduce_only=True,
                )
            )
            if not result.success:
                status = await self.executor.get_order_by_client_id(
                    trade.symbol, client_order_id
                )
                if status is None:
                    return CloseReceipt(False, result.order_id, 0.0, 0.0, False)
            else:
                status = await self.executor.get_order_status(
                    trade.symbol, result.order_id
                )
            if status is None:
                status = await self.executor.get_order_by_client_id(
                    trade.symbol, client_order_id
                )
        if not status or str(status.get("status", "")).upper() != "FILLED":
            return CloseReceipt(
                False,
                str((status or {}).get("orderId", existing_order_id or "")),
                float((status or {}).get("avgPrice", 0.0) or 0.0),
                float((status or {}).get("executedQty", 0.0) or 0.0),
                False,
            )
        filled = float(status.get("executedQty", 0.0) or 0.0)
        fill_price = float(status.get("avgPrice", 0.0) or 0.0)
        flat = self.position_is_flat(trade.symbol)
        if hasattr(flat, "__await__"):
            flat = await flat
        return CloseReceipt(
            confirmed=bool(flat) and filled + 1e-12 >= trade.quantity,
            order_id=str(status.get("orderId", existing_order_id or "")),
            fill_price=fill_price,
            filled_quantity=filled,
            position_flat=bool(flat),
        )


class PositionSwitchCoordinator:
    """Close-confirm-release-authorize-open state machine.

    OPEN_SUBMITTING is intentionally a manual-reconciliation boundary after a
    crash: if the resulting trade cannot be found by opportunity id, this
    coordinator refuses to submit again because the exchange may have filled
    the first request while the process was down.
    """

    def __init__(
        self,
        *,
        lifecycle: Any,
        gateway: CapitalGateway,
        journal: SwitchJournal,
        close_and_confirm: CloseAndConfirm,
        portfolio_provider: PortfolioProvider,
        evaluate_pair: PairEvaluator | None = None,
    ) -> None:
        self.lifecycle = lifecycle
        self.gateway = gateway
        self.journal = journal
        self.close_and_confirm = close_and_confirm
        self.portfolio_provider = portfolio_provider
        self.evaluate_pair = evaluate_pair
        self._locks: dict[str, asyncio.Lock] = {}

    async def evaluate_scan_cycle(
        self, candidates: list[Opportunity]
    ) -> list[tuple[Trade, Opportunity, Any]]:
        """Compare every eligible scan-cycle candidate with every open incumbent.

        The evidence callback must use current PositionManager/market state and
        venue cost estimates. No fallback heuristic is used when it is absent.
        """
        if self.evaluate_pair is None or not candidates:
            return []
        ranked: list[tuple[float, str, Trade, Opportunity, Any]] = []
        for incumbent in self.lifecycle.get_open_trades():
            if incumbent.state != TradeLifecycleState.POSITION_OPENED:
                continue
            for candidate in candidates:
                if candidate.symbol.upper() == incumbent.symbol.upper():
                    continue
                result = self.evaluate_pair(incumbent, candidate)
                decision = await result if hasattr(result, "__await__") else result
                if getattr(decision, "should_switch", False):
                    advantage = float(getattr(decision, "net_advantage_pct", 0.0))
                    ranked.append(
                        (
                            advantage,
                            candidate.opportunity_id,
                            incumbent,
                            candidate,
                            decision,
                        )
                    )
        ranked.sort(key=lambda item: (-item[0], item[1], item[2].trade_id))
        return [
            (incumbent, candidate, decision)
            for _, _, incumbent, candidate, decision in ranked
        ]

    @staticmethod
    def switch_id(incumbent: Trade, candidate: Opportunity) -> str:
        return f"switch:{incumbent.trade_id}:{candidate.opportunity_id}"

    async def _portfolio(self) -> Any:
        result = self.portfolio_provider()
        return await result if hasattr(result, "__await__") else result

    def _find_candidate_trade(self, opportunity_id: str) -> Trade | None:
        trades = [
            *self.lifecycle.get_open_trades(),
            *self.lifecycle.get_closed_trades(),
        ]
        return next(
            (
                trade
                for trade in trades
                if str(trade.agent_consensus.get("opportunity_id", ""))
                == opportunity_id
                or str(trade.agent_consensus.get("decision_id", "")) == opportunity_id
            ),
            None,
        )

    async def switch(
        self,
        *,
        incumbent: Trade,
        candidate: Opportunity,
        decision: Any,
    ) -> Trade | None:
        switch_id = self.switch_id(incumbent, candidate)
        existing_intent = self.journal.get(switch_id)
        if existing_intent is not None and existing_intent.state == "completed":
            return self._find_candidate_trade(candidate.opportunity_id)
        if existing_intent is not None and existing_intent.state in {
            "manual_reconciliation_required",
            "failed",
        }:
            raise RuntimeError(
                f"switch requires operator reconciliation: {existing_intent.reason}"
            )
        resume_after_close = existing_intent is not None and (
            existing_intent.state
            in {
                "close_submitting",
                "close_confirmed",
                "reservation_released",
                "open_ready",
                "open_submitting",
            }
            or (
                existing_intent.state == "close_pending"
                and bool(existing_intent.reason or existing_intent.close_order_id)
            )
        )
        if not resume_after_close:
            if not getattr(decision, "should_switch", False):
                return None
            if self.evaluate_pair is None:
                raise RuntimeError(
                    "fresh incumbent/candidate evidence callback is required for switching"
                )
            fresh_result = self.evaluate_pair(incumbent, candidate)
            decision = (
                await fresh_result
                if hasattr(fresh_result, "__await__")
                else fresh_result
            )
            if not getattr(decision, "should_switch", False):
                return None
            if (
                getattr(decision, "incumbent_symbol", incumbent.symbol).upper()
                != incumbent.symbol.upper()
                or getattr(decision, "candidate_symbol", candidate.symbol).upper()
                != candidate.symbol.upper()
            ):
                raise ValueError(
                    "rebalance decision identity does not match switch pair"
                )
            if opportunity_age_seconds(candidate.detected_at) > (
                self.gateway.controls.opportunity_max_age_seconds
            ):
                return None
            if incumbent.state != TradeLifecycleState.POSITION_OPENED:
                raise ValueError("incumbent must be an open position")
            other_incumbents = [
                trade
                for trade in self.lifecycle.get_open_trades()
                if trade.trade_id != incumbent.trade_id
                and trade.symbol.upper() == incumbent.symbol.upper()
            ]
            if other_incumbents:
                raise RuntimeError(
                    "symbol-scoped capital reservation cannot safely release one of "
                    "multiple same-symbol positions"
                )
            if any(
                trade.symbol.upper() == candidate.symbol.upper()
                for trade in self.lifecycle.get_open_trades()
            ):
                raise RuntimeError("replacement symbol already has an open position")
        if candidate.symbol.upper() == incumbent.symbol.upper():
            raise ValueError("cannot switch into the same instrument")
        if candidate.is_production:
            raise PermissionError(
                "automatic position switching is disabled for production opportunities"
            )

        lock = self._locks.setdefault(switch_id, asyncio.Lock())
        async with lock:
            intent = self.journal.get(switch_id)
            if intent is None:
                pending = [
                    item
                    for item in self.journal.list_incomplete()
                    if item.incumbent_trade_id == incumbent.trade_id
                ]
                if pending:
                    raise RuntimeError(
                        "an earlier switch for this incumbent is still pending "
                        "reconciliation"
                    )
                intent = self.journal.create(
                    SwitchIntent(
                        switch_id=switch_id,
                        incumbent_trade_id=incumbent.trade_id,
                        incumbent_symbol=incumbent.symbol,
                        candidate_opportunity_id=candidate.opportunity_id,
                        candidate_symbol=candidate.symbol,
                        state="close_pending",
                        close_client_order_id=f"aitos-switch-{hashlib.sha256(switch_id.encode()).hexdigest()[:20]}",
                        candidate_payload={
                            "symbol": candidate.symbol,
                            "side": candidate.side.value,
                            "entry_price": candidate.entry_price,
                            "stop_loss_price": candidate.stop_loss_price,
                            "take_profit_levels": list(candidate.take_profit_levels),
                            "confidence": candidate.confidence,
                            "strategy_id": candidate.strategy_id,
                            "rationale": candidate.rationale,
                            "agent_consensus": dict(candidate.agent_consensus),
                            "is_production": candidate.is_production,
                            "approved_by": candidate.approved_by,
                            "trailing_sl_enabled": candidate.trailing_sl_enabled,
                            "breakeven_at_r_multiple": candidate.breakeven_at_r_multiple,
                            "regime": candidate.regime,
                            "opportunity_id": candidate.opportunity_id,
                            "detected_at": candidate.detected_at,
                        },
                    )
                )

            if intent.state == "completed":
                return self._find_candidate_trade(candidate.opportunity_id)
            if intent.state in {"manual_reconciliation_required", "failed"}:
                raise RuntimeError(
                    f"switch requires operator reconciliation: {intent.reason}"
                )

            if intent.state == "close_pending":
                intent = self.journal.update(switch_id, state="close_submitting")

            if intent.state == "close_submitting":
                receipt = await self.close_and_confirm(
                    incumbent, intent.close_client_order_id, intent.close_order_id
                )
                if not receipt.confirmed or not receipt.position_flat:
                    self.journal.update(
                        switch_id,
                        state="close_pending",
                        close_order_id=receipt.order_id or intent.close_order_id,
                        reason="reduce_only_close_not_yet_confirmed_flat",
                    )
                    return None
                if receipt.filled_quantity + 1e-12 < incumbent.quantity:
                    self.journal.update(
                        switch_id,
                        state="close_pending",
                        close_order_id=receipt.order_id,
                        reason="close_fill_less_than_remaining_quantity",
                    )
                    return None
                intent = self.journal.update(
                    switch_id,
                    state="close_confirmed",
                    close_order_id=receipt.order_id,
                    close_fill_price=receipt.fill_price,
                    reason=None,
                )

            if intent.state == "close_confirmed":
                open_trade = next(
                    (
                        item
                        for item in self.lifecycle.get_open_trades()
                        if item.trade_id == incumbent.trade_id
                    ),
                    None,
                )
                if open_trade is not None:
                    if getattr(
                        getattr(self.lifecycle, "_order_executor", None),
                        "requires_exchange_execution",
                        False,
                    ):
                        await self.lifecycle.finalize_confirmed_flat_close(
                            incumbent.trade_id,
                            CloseReceipt(
                                True,
                                str(intent.close_order_id),
                                float(intent.close_fill_price),
                                incumbent.quantity,
                                True,
                            ),
                            intent.close_client_order_id,
                        )
                    else:
                        await self.lifecycle.close_trade(
                            incumbent.trade_id,
                            float(intent.close_fill_price),
                            f"capital_rebalance:{switch_id}",
                        )
                self.gateway.release(incumbent.symbol)
                intent = self.journal.update(switch_id, state="reservation_released")

            if intent.state == "reservation_released":
                intent = self.journal.update(switch_id, state="open_ready")

            if intent.state == "open_submitting":
                existing = self._find_candidate_trade(candidate.opportunity_id)
                if existing is not None:
                    self.journal.update(switch_id, state="completed", reason=None)
                    return existing
                # Never blindly retry after a crash in the open window. Venue
                # order/position reconciliation must first establish no fill.
                self.journal.update(
                    switch_id,
                    state="manual_reconciliation_required",
                    reason="open_submission_outcome_unknown_after_restart",
                )
                raise RuntimeError(
                    "replacement open outcome is unknown; reconcile exchange order "
                    "and position before any retry"
                )

            if intent.state == "open_ready":
                if opportunity_age_seconds(candidate.detected_at) > (
                    self.gateway.controls.opportunity_max_age_seconds
                ):
                    self.journal.update(
                        switch_id,
                        state="failed",
                        reason="candidate_expired_after_incumbent_close",
                    )
                    return None
                self.journal.update(switch_id, state="open_submitting")
                portfolio = await self._portfolio()
                from dataclasses import replace

                identified_candidate = replace(
                    candidate,
                    agent_consensus={
                        **candidate.agent_consensus,
                        "opportunity_id": candidate.opportunity_id,
                        "switch_id": switch_id,
                    },
                )
                trade = await self.lifecycle.submit_opportunity(
                    identified_candidate, portfolio
                )
                if trade.state == TradeLifecycleState.REJECTED:
                    self.journal.update(
                        switch_id,
                        state="failed",
                        reason=trade.rejection_reason or trade.state.value,
                    )
                    return trade
                if trade.state != TradeLifecycleState.POSITION_OPENED:
                    self.journal.update(
                        switch_id,
                        state="manual_reconciliation_required",
                        reason=f"replacement_open_not_confirmed:{trade.state.value}",
                    )
                    raise RuntimeError(
                        "replacement open was not confirmed; reconcile venue order "
                        "and position before retrying"
                    )
                self.journal.update(switch_id, state="completed", reason=None)
                return trade

            return None
