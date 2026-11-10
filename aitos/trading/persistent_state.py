"""Durable runtime state for production trading recovery and risk tracking."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import weakref
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from numbers import Real
from typing import Any

from aitos.app import LivePortfolioTracker
from aitos.data.ingestion import DataIngestionService
from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.execution.position_integrity import assert_position_snapshot_matches
from aitos.execution.order_executor import OrderExecutor, OrderRequest, OrderResult
from aitos.logging_setup import get_logger
from aitos.models.trade import PartialExit, Trade, TradeLifecycleState, TradeSide

logger = get_logger("aitos.trading.persistent_state")

CREATE_RUNTIME_STATE = """
CREATE TABLE IF NOT EXISTS trade_runtime_state (
    trade_id String,
    symbol LowCardinality(String),
    state LowCardinality(String),
    payload_json String,
    updated_at DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY trade_id
"""

CREATE_DRAWDOWN_STATE = """
CREATE TABLE IF NOT EXISTS portfolio_drawdown_state (
    asset LowCardinality(String),
    time DateTime64(3, 'UTC'),
    equity_usd Float64,
    peak_equity_usd Float64,
    drawdown_pct Float64
) ENGINE = ReplacingMergeTree(time)
ORDER BY asset
"""

CREATE_PAPER_CLOSED_TRADE_LEDGER = """
CREATE TABLE IF NOT EXISTS paper_closed_trade_ledger (
    trade_id String,
    payload_json String,
    updated_at DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY trade_id
"""

CREATE_ORDER_EXECUTION_LEDGER = """
CREATE TABLE IF NOT EXISTS order_execution_ledger (
    client_order_id String,
    symbol LowCardinality(String),
    state LowCardinality(String),
    request_json String,
    result_json String,
    updated_at DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY client_order_id
"""


CREATE_EXCHANGE_INCOME_CURSOR = """
CREATE TABLE IF NOT EXISTS exchange_income_sync_cursor (
    account LowCardinality(String),
    cursor_time_ms UInt64,
    updated_at DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY account
"""


# Static, reviewable SQL statements. Filter only AFTER selecting latest
# retained payloads; timestamp ties are not silently resolved with argMax.
# User-controlled values remain driver-bound parameters, never SQL fragments.
CLOSED_HISTORY_QUERY = """
SELECT trade_id, latest_payload, payload_versions
FROM (
    SELECT trade_id, any(payload_json) AS latest_payload,
           uniqExact(payload_json) AS payload_versions, max(updated_at) AS latest
    FROM paper_closed_trade_ledger
    WHERE (trade_id, updated_at) IN (
        SELECT trade_id, max(updated_at)
        FROM paper_closed_trade_ledger
        GROUP BY trade_id
    )
    GROUP BY trade_id
)
WHERE (payload_versions != 1 OR {symbol:String} = ''
       OR JSONExtractString(latest_payload, 'symbol') = {symbol:String})
ORDER BY latest DESC, trade_id
LIMIT {limit:UInt32} OFFSET {offset:UInt32}
SETTINGS max_execution_time=3, max_bytes_to_read=67108864,
         max_memory_usage=134217728, read_overflow_mode='throw'
"""

CLOSED_PERFORMANCE_QUERY = """
SELECT trade_id, latest_payload, payload_versions
FROM (
    SELECT trade_id, any(payload_json) AS latest_payload,
           uniqExact(payload_json) AS payload_versions, max(updated_at) AS latest
    FROM paper_closed_trade_ledger
    WHERE (trade_id, updated_at) IN (
        SELECT trade_id, max(updated_at)
        FROM paper_closed_trade_ledger
        GROUP BY trade_id
    )
    GROUP BY trade_id
)
ORDER BY latest DESC, trade_id
LIMIT {limit:UInt32}
SETTINGS max_execution_time=3, max_bytes_to_read=67108864,
         max_memory_usage=134217728, read_overflow_mode='throw'
"""

CLOSED_RECOVERY_QUERY = """
SELECT trade_id, latest_payload, payload_versions
FROM (
    SELECT trade_id, any(payload_json) AS latest_payload,
           uniqExact(payload_json) AS payload_versions, max(updated_at) AS latest
    FROM paper_closed_trade_ledger
    WHERE (trade_id, updated_at) IN (
        SELECT trade_id, max(updated_at)
        FROM paper_closed_trade_ledger
        GROUP BY trade_id
    )
    GROUP BY trade_id
)
ORDER BY trade_id
"""


def _closed_payload(row) -> dict:
    if len(row) != 3 or row[2] != 1:
        raise ValueError("ambiguous latest closed-ledger payload")
    payload = json.loads(row[1])
    if not isinstance(payload, dict) or payload.get("trade_id") != row[0]:
        raise ValueError("closed-ledger identity mismatch")
    if payload.get("state") != "position_closed":
        raise ValueError("closed ledger contains a nonterminal trade")
    pnl = payload.get("pnl")
    if isinstance(pnl, bool) or not isinstance(pnl, Real) or not math.isfinite(pnl):
        raise ValueError("closed ledger requires finite recorded PnL")
    for key in ("gross_realized_pnl_usd", "fees_paid_usd", "funding_pnl_usd"):
        value = payload.get(key)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, Real)
            or not math.isfinite(value)
        ):
            raise ValueError("invalid closed-ledger cost evidence")
    return payload


_INGESTIONS: weakref.WeakSet[DataIngestionService] = weakref.WeakSet()


def _install_ingestion_registry() -> None:
    """Track ingestion instances so restored positions can be resubscribed.

    TradeStatePersistence is imported before the application builds its
    DataIngestionService. Registering instances here avoids a startup ordering
    race where restored positions exist in lifecycle state but never enter the
    live price universe.
    """
    if getattr(DataIngestionService, "_aitos_persistence_registry_installed", False):
        return
    original_init = DataIngestionService.__init__

    def tracked_init(self: DataIngestionService, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        _INGESTIONS.add(self)

    DataIngestionService.__init__ = tracked_init  # type: ignore[method-assign]
    DataIngestionService._aitos_persistence_registry_installed = True  # type: ignore[attr-defined]


_install_ingestion_registry()


class DurableTradingStateStore:
    """Small ClickHouse-backed state store used by live trading recovery."""

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    async def initialize(self) -> None:
        if self._repository is None or self._repository._client is None:
            logger.warning(
                "durable trading state store skipped: ClickHouse repository unavailable"
            )
            return
        try:
            await self._repository._client.command(CREATE_RUNTIME_STATE)
            await self._repository._client.command(CREATE_DRAWDOWN_STATE)
            await self._repository._client.command(CREATE_PAPER_CLOSED_TRADE_LEDGER)
            await self._repository._client.command(CREATE_EXCHANGE_INCOME_CURSOR)
            await self._repository._client.command(CREATE_ORDER_EXECUTION_LEDGER)
            logger.info("durable trading state tables ensured")
        except Exception:
            logger.exception("durable trading state table ensure failed")
            raise

    async def load_income_cursor(self, account: str = "binance_usdm") -> int | None:
        client = self._client()
        result = await client.query(
            """
            SELECT argMax(cursor_time_ms, updated_at)
            FROM exchange_income_sync_cursor
            WHERE account = {account:String}
            """,
            parameters={"account": account},
        )
        if not result.result_rows or result.result_rows[0][0] is None:
            return None
        return int(result.result_rows[0][0])

    async def save_income_cursor(
        self, cursor_time_ms: int, account: str = "binance_usdm"
    ) -> None:
        if cursor_time_ms < 0:
            raise ValueError("income cursor must be non-negative")
        client = self._client()
        await client.insert(
            "exchange_income_sync_cursor",
            [[account, int(cursor_time_ms), datetime.now(timezone.utc)]],
            column_names=["account", "cursor_time_ms", "updated_at"],
        )

    async def save_order_intent(
        self, client_order_id: str, request: Mapping[str, Any]
    ) -> None:
        """Durably record an order intent before any exchange side effect."""
        if not client_order_id:
            raise ValueError("client_order_id is required")
        client = self._client()
        symbol = str(request.get("symbol") or "")
        if not symbol:
            raise ValueError("order intent symbol is required")
        await client.insert(
            "order_execution_ledger",
            [
                [
                    client_order_id,
                    symbol,
                    "SUBMITTING",
                    json.dumps(dict(request), sort_keys=True, default=str),
                    "",
                    datetime.now(timezone.utc),
                ]
            ],
            column_names=[
                "client_order_id",
                "symbol",
                "state",
                "request_json",
                "result_json",
                "updated_at",
            ],
        )

    async def save_order_result(
        self, client_order_id: str, symbol: str, result: Mapping[str, Any]
    ) -> None:
        """Persist the authoritative acknowledgement/fill returned by the executor."""
        client = self._client()
        payload = dict(result)
        existing = await self.load_order_record(client_order_id)
        request_json = json.dumps(
            existing.get("request", {}) if existing else {},
            sort_keys=True,
            default=str,
        )
        state = (
            "ACKNOWLEDGED"
            if (
                bool(payload.get("success"))
                or float(payload.get("filled_quantity", 0) or 0) > 0
            )
            else "REJECTED"
        )
        await client.insert(
            "order_execution_ledger",
            [
                [
                    client_order_id,
                    symbol,
                    state,
                    request_json,
                    json.dumps(payload, sort_keys=True, default=str),
                    datetime.now(timezone.utc),
                ]
            ],
            column_names=[
                "client_order_id",
                "symbol",
                "state",
                "request_json",
                "result_json",
                "updated_at",
            ],
        )

    async def load_pending_order_intents(self) -> list[dict[str, Any]]:
        """Return latest order intents whose venue outcome is not yet terminal."""
        client = self._client()
        result = await client.query(
            """
            SELECT
                client_order_id,
                argMax(symbol, updated_at) AS symbol,
                argMax(state, updated_at) AS state,
                argMax(request_json, updated_at) AS request_json
            FROM order_execution_ledger
            GROUP BY client_order_id
            HAVING state = 'SUBMITTING'
            ORDER BY client_order_id
            """
        )
        pending = []
        for row in result.result_rows:
            pending.append(
                {
                    "client_order_id": str(row[0]),
                    "symbol": str(row[1]),
                    "state": str(row[2]),
                    "request": json.loads(row[3] or "{}"),
                }
            )
        return pending

    async def load_unlinked_acknowledged_orders(self) -> list[str]:
        """Find acknowledged entry fills lacking any open or closed trade snapshot."""
        client = self._client()
        orders = await client.query(
            """
            SELECT
                client_order_id,
                argMax(state, updated_at) AS state,
                argMax(result_json, updated_at) AS result_json
            FROM order_execution_ledger
            GROUP BY client_order_id
            HAVING state = 'ACKNOWLEDGED'
            """
        )
        trade_rows = await client.query(
            """
            SELECT trade_id FROM trade_runtime_state
            UNION DISTINCT
            SELECT trade_id FROM paper_closed_trade_ledger
            """
        )
        known_trade_ids = {str(row[0]) for row in trade_rows.result_rows}
        unlinked = []
        for row in orders.result_rows:
            client_order_id = str(row[0])
            if not client_order_id.startswith("aitos-entry-"):
                continue
            trade_id = client_order_id.removeprefix("aitos-entry-")
            if trade_id not in known_trade_ids:
                # Even an accepted zero-fill order may still be resting at the
                # venue and fill after this process exits. Require reconciliation.
                unlinked.append(client_order_id)
        return sorted(unlinked)

    async def load_order_record(self, client_order_id: str) -> dict[str, Any] | None:
        """Load the latest durable order-intent/acknowledgement state."""
        client = self._client()
        result = await client.query(
            """
            SELECT
                argMax(symbol, updated_at) AS symbol,
                argMax(state, updated_at) AS state,
                argMax(request_json, updated_at) AS request_json,
                argMax(result_json, updated_at) AS result_json
            FROM order_execution_ledger
            WHERE client_order_id = {client_order_id:String}
            """,
            parameters={"client_order_id": client_order_id},
        )
        if not result.result_rows or not result.result_rows[0][1]:
            return None
        row = result.result_rows[0]
        return {
            "symbol": str(row[0]),
            "state": str(row[1]),
            "request": json.loads(row[2] or "{}"),
            "result": json.loads(row[3] or "{}"),
        }

    async def save_trade(self, trade: Trade) -> None:
        client = self._client()
        payload = trade.to_dict()
        try:
            await client.insert(
                "trade_runtime_state",
                [
                    [
                        trade.trade_id,
                        trade.symbol,
                        trade.state.value,
                        json.dumps(
                            payload, sort_keys=True, default=str, allow_nan=False
                        ),
                        datetime.fromisoformat(trade.updated_at.replace("Z", "+00:00")),
                    ]
                ],
                column_names=[
                    "trade_id",
                    "symbol",
                    "state",
                    "payload_json",
                    "updated_at",
                ],
            )
        except Exception:
            logger.exception(
                "failed to save trade runtime state",
                extra={
                    "aitos_extra": {
                        "trade_id": trade.trade_id,
                        "symbol": trade.symbol,
                        "state": trade.state.value,
                    }
                },
            )
            raise

    async def delete_trade(self, trade_id: str) -> None:
        client = self._client()
        try:
            await client.command(
                "ALTER TABLE trade_runtime_state DELETE "
                "WHERE trade_id = {trade_id:String}",
                parameters={"trade_id": trade_id},
            )
        except Exception:
            logger.exception(
                "failed to delete trade runtime state",
                extra={"aitos_extra": {"trade_id": trade_id}},
            )
            raise

    async def load_open_trades(self) -> list[Trade]:
        client = self._client()
        try:
            result = await client.query(
                """
                SELECT trade_id, any(payload_json) AS latest_payload,
                       uniqExact(payload_json) AS latest_payload_versions
                FROM trade_runtime_state
                WHERE (trade_id, updated_at) IN (
                    SELECT trade_id, max(updated_at)
                    FROM trade_runtime_state GROUP BY trade_id
                ) AND trade_id NOT IN (
                    SELECT trade_id FROM paper_closed_trade_ledger
                )
                GROUP BY trade_id
                """
            )
        except Exception:
            logger.exception("failed to load open trades from runtime state")
            raise
        trades: list[Trade] = []
        for row in result.result_rows:
            try:
                if len(row) != 3 or row[2] != 1:
                    raise ValueError("ambiguous latest runtime payload")
                payload = json.loads(row[1])
                if payload.get("trade_id") != row[0]:
                    raise ValueError("runtime payload identity mismatch")
                trade = _trade_from_dict(payload)
                if trade.state in {
                    TradeLifecycleState.POSITION_OPENED,
                    TradeLifecycleState.EXIT_TRIGGERED,
                }:
                    trades.append(trade)
            except Exception as exc:
                logger.error(
                    "runtime recovery blocked by invalid or ambiguous state",
                    extra={"aitos_extra": {"trade_id": row[0] if row else None}},
                )
                raise RuntimeError(
                    "runtime recovery requires unambiguous valid latest trade state"
                ) from exc
        return trades

    async def save_closed_trade(self, trade: Trade) -> None:
        """Archive a terminal paper trade before removing its open-state row."""
        if trade.state != TradeLifecycleState.POSITION_CLOSED:
            raise ValueError("only closed trades can be archived")
        client = self._client()
        _closed_payload(
            (trade.trade_id, json.dumps(trade.to_dict(), allow_nan=False), 1)
        )
        await client.insert(
            "paper_closed_trade_ledger",
            [
                [
                    trade.trade_id,
                    json.dumps(
                        trade.to_dict(), sort_keys=True, default=str, allow_nan=False
                    ),
                    datetime.now(timezone.utc),
                ]
            ],
            column_names=["trade_id", "payload_json", "updated_at"],
        )

    async def query_closed_history(
        self, *, limit: int, offset: int = 0, symbol: str = ""
    ) -> list[dict]:
        """Bounded parameterized query over the existing durable closed ledger."""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 1 <= limit <= 100
            or not 0 <= offset <= 100000
        ):
            raise ValueError("invalid bounded history query")
        result = await self._client().query(
            CLOSED_HISTORY_QUERY,
            parameters={"symbol": symbol, "limit": limit, "offset": offset},
        )
        return [_closed_payload(row) for row in result.result_rows]

    async def query_closed_snapshot(self, *, limit: int = 10000) -> dict:
        """One bounded result for explicit console materialization, no source writes."""
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("invalid bounded snapshot query")
        result = await self._client().query(
            CLOSED_HISTORY_QUERY,
            parameters={"symbol": "", "limit": limit + 1, "offset": 0},
        )
        # Validate the extra row too; never hide a known revision conflict.
        payloads = [_closed_payload(row) for row in result.result_rows]
        return {"rows": payloads[:limit], "truncated": len(payloads) > limit}

    async def query_closed_performance(self, *, limit: int = 1000) -> dict:
        """One bounded query, not a loop over offset pages of a changing ledger."""
        from aitos.trading.performance import analyze_closed_trades

        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10000
        ):
            raise ValueError("invalid bounded performance query")
        result = await self._client().query(
            CLOSED_PERFORMANCE_QUERY,
            parameters={"limit": limit + 1},
        )
        rows = result.result_rows
        # Validate the extra row too: a known conflict must not be hidden.
        payloads = [_closed_payload(row) for row in rows]
        return analyze_closed_trades(
            payloads[:limit], window_limit=limit, truncated=len(rows) > limit
        )

    async def load_closed_trades(self) -> list[Trade]:
        """Restore complete realized evidence or fail; never silently lose PnL."""
        result = await self._client().query(CLOSED_RECOVERY_QUERY)
        try:
            return [
                _trade_from_dict(_closed_payload(row)) for row in result.result_rows
            ]
        except Exception as exc:
            raise RuntimeError(
                "closed-ledger recovery requires valid unambiguous terminal records"
            ) from exc

    async def save_drawdown(self, asset: str, equity: float, peak: float) -> None:
        client = self._client()
        drawdown = ((peak - equity) / peak * 100.0) if peak > 0 else 0.0
        try:
            await client.insert(
                "portfolio_drawdown_state",
                [[asset, datetime.now(timezone.utc), equity, peak, drawdown]],
                column_names=[
                    "asset",
                    "time",
                    "equity_usd",
                    "peak_equity_usd",
                    "drawdown_pct",
                ],
            )
        except Exception:
            logger.exception(
                "failed to save portfolio drawdown state",
                extra={
                    "aitos_extra": {
                        "asset": asset,
                        "equity": equity,
                        "peak": peak,
                        "drawdown_pct": drawdown,
                    }
                },
            )
            raise

    async def load_peak_equity(self, asset: str) -> float | None:
        client = self._client()
        try:
            result = await client.query(
                """
                SELECT argMax(peak_equity_usd, time) AS peak_equity_usd
                FROM portfolio_drawdown_state
                WHERE asset = {asset:String}
                """,
                parameters={"asset": asset},
            )
        except Exception:
            logger.exception(
                "failed to load peak equity",
                extra={"aitos_extra": {"asset": asset}},
            )
            raise
        if not result.result_rows or result.result_rows[0][0] is None:
            return None
        return float(result.result_rows[0][0])

    def _client(self) -> Any:
        if self._repository is None or self._repository._client is None:
            raise RuntimeError(
                "durable trading state requires an initialized ClickHouse repository"
            )
        return self._repository._client


def _trade_from_dict(data: dict[str, Any]) -> Trade:
    partial_exits = [
        PartialExit(
            price=float(item["price"]),
            size_usd=float(item["size_usd"]),
            r_multiple=float(item["r_multiple"]),
            at=str(item.get("at") or datetime.now(timezone.utc).isoformat()),
        )
        for item in data.get("partial_exits", [])
    ]
    return Trade(
        trade_id=str(data["trade_id"]),
        symbol=str(data["symbol"]),
        side=TradeSide(str(data["side"])),
        entry_price=float(data["entry_price"]),
        quantity=float(data.get("quantity", 0.0)),
        leverage=float(data.get("leverage", 0.0)),
        position_size_usd=float(data.get("position_size_usd", 0.0)),
        risk_amount_usd=float(data.get("risk_amount_usd", 0.0)),
        strategy_id=str(data.get("strategy_id", "recovered")),
        agent_consensus=dict(data.get("agent_consensus", {})),
        explanation=str(data.get("explanation", "recovered trade")),
        sl_price=float(data["sl_price"]),
        tp_price=float(data.get("tp_price", data["entry_price"])),
        state=TradeLifecycleState(str(data.get("state", "position_opened"))),
        entry_time=str(data["entry_time"]),
        trailing_sl_enabled=bool(data.get("trailing_sl_enabled", False)),
        breakeven_triggered=bool(data.get("breakeven_triggered", False)),
        breakeven_at_r_multiple=(
            float(data["breakeven_at_r_multiple"])
            if data.get("breakeven_at_r_multiple") is not None
            else None
        ),
        take_profit_levels=[float(x) for x in data.get("take_profit_levels", [])],
        partial_exits=partial_exits,
        sl_order_id=data.get("sl_order_id"),
        tp_order_ids=[str(x) for x in data.get("tp_order_ids", [])],
        regime=str(data.get("regime", "unknown")),
        exit_price=data.get("exit_price"),
        exit_time=data.get("exit_time"),
        exit_reason=data.get("exit_reason"),
        pnl=data.get("pnl"),
        pnl_percent=data.get("pnl_percent"),
        gross_realized_pnl_usd=float(
            data.get("gross_realized_pnl_usd", data.get("pnl") or 0.0)
        ),
        fees_paid_usd=float(data.get("fees_paid_usd", 0.0)),
        funding_pnl_usd=float(data.get("funding_pnl_usd", 0.0)),
        cost_adjustments=dict(data.get("cost_adjustments", {})),
        rejection_reason=data.get("rejection_reason"),
        entry_thesis=(
            dict(data["entry_thesis"])
            if isinstance(data.get("entry_thesis"), dict)
            else None
        ),
        emergency_sl_price=(
            float(data["emergency_sl_price"])
            if data.get("emergency_sl_price") is not None
            else None
        ),
        adaptive_exit_observations=max(
            0, int(data.get("adaptive_exit_observations", 0))
        ),
        adaptive_exit_last_bar_id=(
            str(data["adaptive_exit_last_bar_id"])
            if data.get("adaptive_exit_last_bar_id") is not None
            else None
        ),
        initial_r_distance=(
            float(data["initial_r_distance"])
            if data.get("initial_r_distance") is not None
            else None
        ),
        mae_price=(
            float(data["mae_price"]) if data.get("mae_price") is not None else None
        ),
        mfe_price=(
            float(data["mfe_price"]) if data.get("mfe_price") is not None else None
        ),
        mae_r=(float(data["mae_r"]) if data.get("mae_r") is not None else None),
        mfe_r=(float(data["mfe_r"]) if data.get("mfe_r") is not None else None),
        last_marked_at=(
            str(data["last_marked_at"])
            if data.get("last_marked_at") is not None
            else None
        ),
        updated_at=str(data.get("updated_at") or data["entry_time"]),
    )


def restored_position_feeds_ready(
    restored_open_count: int, subscription_result: dict[str, Any] | None
) -> bool:
    """Fail closed when restored positions are not covered by verified feeds.

    An empty portfolio needs no position-specific subscriptions. Once at least
    one persisted position is restored, every registered ingestion instance
    must confirm both trade and kline coverage before a trading loop may start.
    """
    if restored_open_count == 0:
        return True
    if restored_open_count < 0 or not isinstance(subscription_result, dict):
        return False

    symbols = subscription_result.get("symbols")
    try:
        ingestion_count = int(subscription_result.get("ingestions", 0))
        verified_count = int(subscription_result.get("verified", 0))
        failure_count = int(subscription_result.get("failures", 0))
    except (TypeError, ValueError):
        return False

    return bool(
        isinstance(symbols, list)
        and symbols
        and ingestion_count > 0
        and verified_count == ingestion_count
        and failure_count == 0
    )


class TradeStatePersistence:
    """Persist lifecycle events and restore open positions before streams start."""

    def __init__(
        self,
        event_bus: EventBus,
        lifecycle: Any,
        store: DurableTradingStateStore,
    ) -> None:
        self._event_bus = event_bus
        self._lifecycle = lifecycle
        self._store = store
        self._subscriptions: list[Subscription] = []

    async def restore(self) -> int:
        """Restore durable lifecycle state without assuming market streams are ready.

        The canonical runtime can accept symbol configuration before it starts,
        but that alone does not prove the active feed is subscribed after startup.
        An explicit post-initialization reconciliation verifies effective symbols.
        """
        load_closed = getattr(self._store, "load_closed_trades", None)
        closed_trades = await load_closed() if callable(load_closed) else []
        closed_ids = {trade.trade_id for trade in closed_trades}
        trades = await self._store.load_open_trades()
        restored_open_count = 0
        for trade in trades:
            # A close archive is authoritative if a crash occurred between
            # archiving the close and deleting the stale open-state row.
            if trade.trade_id not in closed_ids:
                self._lifecycle._open_trades[trade.trade_id] = trade
                restored_open_count += 1
        if closed_trades and hasattr(self._lifecycle, "_closed_trades"):
            existing = {trade.trade_id for trade in self._lifecycle._closed_trades}
            self._lifecycle._closed_trades.extend(
                trade for trade in closed_trades if trade.trade_id not in existing
            )

        # Rebuild the statistical shadow ledger from durable trade state after
        # a crash between position persistence and the shadow SQLite write.
        record_forecast = getattr(self._lifecycle, "_record_statistical_forecast", None)
        record_outcome = getattr(self._lifecycle, "_record_statistical_outcome", None)
        if callable(record_forecast):
            recovery_trades = {trade.trade_id: trade for trade in trades}
            recovery_trades.update({trade.trade_id: trade for trade in closed_trades})
            for trade in recovery_trades.values():
                consensus = getattr(trade, "agent_consensus", None)
                forecast = (
                    consensus.get("statistical_forecast")
                    if isinstance(consensus, Mapping)
                    else None
                )
                if not isinstance(forecast, Mapping):
                    continue
                try:
                    await asyncio.to_thread(record_forecast, trade)
                    if trade.state == TradeLifecycleState.POSITION_CLOSED and callable(
                        record_outcome
                    ):
                        await asyncio.to_thread(record_outcome, trade)
                except Exception:
                    logger.exception(
                        "failed to reconcile statistical shadow state from trade journal",
                        extra={"aitos_extra": {"trade_id": trade.trade_id}},
                    )
        if trades:
            symbols = list(
                dict.fromkeys(
                    str(trade.symbol).upper() for trade in trades if trade.symbol
                )
            )
            logger.warning(
                "restored open trades after process restart",
                extra={
                    "aitos_extra": {
                        "count": len(trades),
                        "symbols": symbols,
                        "market_data_resubscriptions": "pending_until_ingestion_initialized",
                    }
                },
            )
        else:
            logger.info("no open trades to restore from durable state")
        return restored_open_count

    async def resubscribe_restored_positions(self) -> dict[str, Any]:
        """Reconcile restored position symbols with initialized live market feeds."""
        trades = list(self._lifecycle.get_open_trades())
        symbols = list(
            dict.fromkeys(str(trade.symbol).upper() for trade in trades if trade.symbol)
        )
        if not symbols:
            return {"symbols": [], "ingestions": 0, "verified": 0, "failures": 0}

        ingestion_count = 0
        verified_count = 0
        failures = 0
        for ingestion in list(_INGESTIONS):
            try:
                # Install protected-position wrappers before this method is called,
                # so scanner subscription churn cannot evict restored symbols.
                await ingestion.update_live_trade_symbols(symbols)
                await ingestion.update_live_kline_symbols(symbols)
                ingestion_count += 1

                live_trade_symbols = {
                    str(s).upper()
                    for s in getattr(ingestion, "_live_trade_symbols", [])
                }
                live_kline_symbols = {
                    str(s).upper()
                    for s in getattr(ingestion, "_live_kline_symbols", [])
                }
                missing_trade = sorted(set(symbols) - live_trade_symbols)
                missing_kline = sorted(set(symbols) - live_kline_symbols)
                if not missing_trade and not missing_kline:
                    verified_count += 1
                else:
                    failures += 1
                    logger.error(
                        "restored position symbols missing from live subscriptions",
                        extra={
                            "aitos_extra": {
                                "symbols": symbols,
                                "missing_trade_symbols": missing_trade,
                                "missing_kline_symbols": missing_kline,
                            }
                        },
                    )
            except Exception as exc:
                failures += 1
                logger.exception(
                    "failed to resubscribe restored position market data",
                    extra={
                        "aitos_extra": {
                            "symbols": symbols,
                            "error": str(exc),
                        }
                    },
                )

        result = {
            "symbols": symbols,
            "ingestions": ingestion_count,
            "verified": verified_count,
            "failures": failures,
        }
        logger.info(
            "restored position market-data reconciliation complete",
            extra={"aitos_extra": result},
        )
        if not _INGESTIONS:
            failures = 1
            result["failures"] = failures
            logger.error(
                "no DataIngestionService registered for restored position recovery",
                extra={"aitos_extra": {"symbols": symbols}},
            )
        return result

    async def initialize(self) -> None:
        await self._store.initialize()
        self._subscriptions.clear()
        self._subscriptions.append(
            await self._event_bus.subscribe(
                "trade.position_opened",
                self._save_event,
                group="trade-state-persistence",
            )
        )
        self._subscriptions.append(
            await self._event_bus.subscribe(
                "trade.position_updated",
                self._save_event,
                group="trade-state-persistence",
            )
        )
        self._subscriptions.append(
            await self._event_bus.subscribe(
                "trade.trailing_sl",
                self._save_event,
                group="trade-state-persistence",
            )
        )
        self._subscriptions.append(
            await self._event_bus.subscribe(
                "trade.partial_close",
                self._save_event,
                group="trade-state-persistence",
            )
        )
        self._subscriptions.append(
            await self._event_bus.subscribe(
                "trade.position_closed",
                self._delete_event,
                group="trade-state-persistence",
            )
        )
        self._lifecycle._execution_state_writer = self.persist_execution_state
        self._lifecycle._execution_position_verifier = self.verify_execution_positions
        logger.info(
            "trade state persistence subscribed",
            extra={"aitos_extra": {"subscriptions": len(self._subscriptions)}},
        )

    async def verify_execution_positions(self) -> None:
        executor = self._lifecycle._order_executor
        try:
            if any(
                trade.agent_consensus.get("_pending_stop_cancellations")
                for trade in self._lifecycle.get_open_trades()
            ):
                raise ValueError(
                    "unresolved protective-order cancellation requires reconciliation"
                )
            assert_position_snapshot_matches(
                self._lifecycle.get_open_trades(),
                await executor.get_position_risk(),
                hedge_mode=executor.hedge_mode,
            )
        except Exception:
            self._lifecycle._account_reconciliation_error = True
            raise
        self._lifecycle._account_reconciliation_error = False

    async def persist_execution_state(self, trade: Trade) -> None:
        """Critical write-through, distinct from asynchronous event publication.

        Archive precedes deletion; a failure keeps the intent unacknowledged.
        """
        if trade.state == TradeLifecycleState.POSITION_CLOSED:
            await self._store.save_closed_trade(trade)
            await self._store.delete_trade(trade.trade_id)
        else:
            await self._store.save_trade(trade)

    async def shutdown(self) -> None:
        drain = getattr(self._lifecycle, "drain_live_submissions", None)
        if callable(drain):
            await drain()
        if (
            getattr(self._lifecycle, "_execution_state_writer", None)
            == self.persist_execution_state
        ):
            del self._lifecycle._execution_state_writer
        if (
            getattr(self._lifecycle, "_execution_position_verifier", None)
            == self.verify_execution_positions
        ):
            del self._lifecycle._execution_position_verifier
        for subscription in self._subscriptions:
            subscription.cancel()
        self._subscriptions.clear()
        logger.info("trade state persistence shut down")

    async def _save_event(self, event: Any) -> None:
        try:
            payload = dict(event.payload)
            trade = _trade_from_dict(payload)
            if trade.state == TradeLifecycleState.POSITION_CLOSED:
                archive = getattr(self._store, "save_closed_trade", None)
                if callable(archive):
                    await archive(trade)
            elif trade.state in (
                TradeLifecycleState.POSITION_OPENED,
                TradeLifecycleState.EXIT_TRIGGERED,
            ):
                await self._store.save_trade(trade)
        except Exception as exc:
            logger.error(
                "trade state persistence failed; propagating for delivery retry",
                extra={
                    "aitos_extra": {
                        "error": str(exc),
                        "event_id": getattr(event, "event_id", None),
                        "topic": getattr(event, "topic", None),
                    }
                },
            )
            raise

    async def _delete_event(self, event: Any) -> None:
        trade_id = event.payload.get("trade_id")
        if not trade_id:
            logger.warning(
                "trade.position_closed without trade_id; skip delete",
                extra={
                    "aitos_extra": {
                        "event_id": getattr(event, "event_id", None),
                    }
                },
            )
            return
        try:
            # The close event carries the complete immutable trade snapshot.
            payload = dict(event.payload)
            closed_trade = _trade_from_dict(payload)
            archive = getattr(self._store, "save_closed_trade", None)
            if callable(archive):
                await archive(closed_trade)
            await self._store.delete_trade(str(trade_id))
        except Exception as exc:
            logger.error(
                "trade state close archival/deletion failed; propagating for delivery retry",
                extra={
                    "aitos_extra": {
                        "trade_id": trade_id,
                        "error": str(exc),
                    }
                },
            )
            raise


class PersistentLivePortfolioTracker(LivePortfolioTracker):
    """Live tracker whose peak equity survives process/container restarts."""

    def __init__(
        self,
        order_executor: Any,
        state_store: DurableTradingStateStore,
        asset: str = "USDT",
    ) -> None:
        super().__init__(order_executor=order_executor, asset=asset)
        self._state_store = state_store

    async def restore(self) -> None:
        peak = await self._state_store.load_peak_equity(self._asset)
        self._peak_equity_usd = peak
        if peak is not None:
            logger.info(
                "restored peak equity from durable state",
                extra={"aitos_extra": {"asset": self._asset, "peak_equity_usd": peak}},
            )

    async def refresh_equity(self) -> float:
        equity = await super().refresh_equity()
        await self._state_store.save_drawdown(
            self._asset,
            equity,
            self._peak_equity_usd or equity,
        )
        return equity


class AmbiguousOrderSubmissionError(RuntimeError):
    """Venue acceptance is uncertain; resubmission is blocked until reconciled."""


class IdempotentOrderExecutor(OrderExecutor):
    """Idempotent submit wrapper with durable intent and acknowledgement recovery.

    The intent is persisted before the exchange side effect. After a crash or
    lost response, recovery queries the venue by client order id and never
    blindly submits the same intent again.
    """

    def __init__(self, inner: OrderExecutor, state_store: Any | None = None) -> None:
        self._inner = inner
        self._state_store = state_store
        self._completed: dict[str, OrderResult] = {}
        self._inflight: dict[str, asyncio.Future[OrderResult]] = {}
        self._requests: dict[str, OrderRequest] = {}
        self._blocked_reason: str | None = None
        self._blocked_client_order_id: str | None = None

    @property
    def supports_exchange_side_stops(self) -> bool:
        return self._inner.supports_exchange_side_stops

    @property
    def requires_exchange_execution(self) -> bool:
        return self._inner.requires_exchange_execution

    def has_unresolved_orders(self, symbol: str | None = None) -> bool:
        return any(
            symbol is None or self._requests[key].symbol == symbol
            for key in self._inflight
        ) or self._inner.has_unresolved_orders(symbol)

    def acknowledge_execution(self, client_order_id: str, trade_id: str) -> None:
        self._inner.acknowledge_execution(client_order_id, trade_id)

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        # Reject malformed economics before registering an identity or causing
        # any venue side effect. NaN bypasses ordinary <= 0 comparisons.
        for name in ("quantity", "reference_price"):
            value = getattr(request, name)
            try:
                valid = (
                    not isinstance(value, bool)
                    and isinstance(value, Real)
                    and math.isfinite(value)
                    and value > 0
                )
            except (TypeError, ValueError, OverflowError):
                valid = False
            if not valid:
                raise ValueError(f"{name} must be finite and strictly positive")
        key = request.client_order_id or self._key(request)
        request = (
            request
            if request.client_order_id
            else replace(request, client_order_id=key)
        )
        if self._blocked_reason and key != self._blocked_client_order_id:
            raise AmbiguousOrderSubmissionError(
                f"new order submissions blocked by unresolved intent "
                f"{self._blocked_client_order_id}: {self._blocked_reason}"
            )
        previous = self._requests.get(key)
        if previous is not None and previous != request:
            raise ValueError("client order ID reused for a different request")
        self._requests[key] = request
        if key in self._completed:
            return self._completed[key]
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._submit_with_recovery(key, request))
            self._inflight[key] = task

            def settle(finished: asyncio.Future[OrderResult]) -> None:
                # Ownership belongs to the submission, not whichever caller
                # happens to be awaiting it. Also retrieve orphaned exceptions.
                self._inflight.pop(key, None)
                if not finished.cancelled() and finished.exception() is None:
                    result = finished.result()
                    if result.success or result.filled_quantity > 0:
                        self._completed[key] = result

            task.add_done_callback(settle)
        return await asyncio.shield(task)

    async def _submit_with_recovery(
        self, key: str, request: OrderRequest
    ) -> OrderResult:
        if self._state_store is None:
            return await self._inner.submit_order(request)

        record = await self._state_store.load_order_record(key)
        if record is not None:
            # A durable client ID is an immutable request identity across
            # process restarts, not merely a lookup key. Never replay an old
            # acknowledgement against changed economics or position intent.
            stored_request = record.get("request")
            if (
                not isinstance(stored_request, Mapping)
                or dict(stored_request) != self._request_payload(request)
                or record.get("symbol") != request.symbol
            ):
                self._blocked_reason = "durable client order ID conflicts with request"
                self._blocked_client_order_id = key
                raise AmbiguousOrderSubmissionError(
                    f"order {key} is bound to a different durable request; manual reconciliation required"
                )
            state = str(record.get("state", "")).upper()
            if state in {"ACKNOWLEDGED", "REJECTED"}:
                try:
                    raw_result = record.get("result")
                    if (
                        not isinstance(raw_result, Mapping)
                        or type(raw_result.get("success")) is not bool
                        or not isinstance(raw_result.get("symbol"), str)
                        or not isinstance(raw_result.get("side"), str)
                        or any(
                            isinstance(raw_result.get(field), bool)
                            or not isinstance(raw_result.get(field), Real)
                            or not math.isfinite(float(raw_result.get(field)))
                            for field in ("filled_quantity", "fill_price")
                        )
                    ):
                        raise ValueError("durable result schema is incomplete")
                    replay = self._result_from_payload(raw_result, request)
                    valid_quantity = (
                        math.isfinite(replay.filled_quantity)
                        and replay.filled_quantity >= 0
                        and replay.filled_quantity <= request.quantity * (1 + 1e-9)
                    )
                    valid_price = math.isfinite(replay.fill_price) and replay.fill_price >= 0
                    if (
                        replay.symbol != request.symbol
                        or replay.side != request.side
                        or not valid_quantity
                        or not valid_price
                        or (
                            state == "ACKNOWLEDGED"
                            and (
                                not replay.success
                                or replay.filled_quantity <= 0
                                or replay.fill_price <= 0
                                or not replay.order_id
                            )
                        )
                        or (
                            state == "REJECTED"
                            and (
                                replay.success
                                or replay.filled_quantity != 0
                                or replay.fill_price != 0
                            )
                        )
                    ):
                        raise ValueError("durable result does not match its terminal state")
                except (TypeError, ValueError, OverflowError):
                    self._blocked_reason = "durable terminal order evidence is invalid"
                    self._blocked_client_order_id = key
                    raise AmbiguousOrderSubmissionError(
                        f"order {key} has invalid durable terminal evidence; manual reconciliation required"
                    ) from None
                self._blocked_reason = None
                self._blocked_client_order_id = None
                return replay
            if state == "SUBMITTING":
                try:
                    recovered = await self._recover_ambiguous(key, request)
                except Exception as exc:
                    self._blocked_reason = str(exc)
                    self._blocked_client_order_id = key
                    raise
                self._blocked_reason = None
                self._blocked_client_order_id = None
                return recovered
            raise AmbiguousOrderSubmissionError(
                f"unsupported durable order state {state!r} for {key}; "
                "manual reconciliation required"
            )

        await self._state_store.save_order_intent(key, self._request_payload(request))
        # An exception or process death here is ambiguous: retain SUBMITTING
        # so the next process must query the venue rather than resubmit.
        try:
            result = await self._inner.submit_order(request)
            await self._state_store.save_order_result(
                key, request.symbol, self._result_payload(result)
            )
        except BaseException as exc:
            self._blocked_reason = str(exc) or type(exc).__name__
            self._blocked_client_order_id = key
            raise
        return result

    async def reconcile_pending_orders(self) -> dict[str, Any]:
        """Reconcile crash-window intents and detect fills without trade snapshots."""
        if self._state_store is None:
            return {"pending": 0, "filled_unlinked": [], "terminal_unfilled": []}
        unlinked = list(await self._state_store.load_unlinked_acknowledged_orders())
        pending = await self._state_store.load_pending_order_intents()
        terminal_unfilled: list[str] = []
        for record in pending:
            payload = record.get("request", {})
            request = OrderRequest(
                symbol=str(payload["symbol"]),
                side=TradeSide(str(payload["side"])),
                quantity=float(payload["quantity"]),
                reference_price=float(payload["reference_price"]),
                order_type=str(payload.get("order_type", "MARKET")),
                client_order_id=str(record["client_order_id"]),
                reduce_only=bool(payload.get("reduce_only", False)),
            )
            result = await self._recover_ambiguous(
                str(record["client_order_id"]), request
            )
            if result.filled_quantity > 0:
                # A recovered fill without a matching durable Trade snapshot
                # cannot safely be attached to portfolio risk automatically.
                unlinked.append(str(record["client_order_id"]))
            else:
                terminal_unfilled.append(str(record["client_order_id"]))
        filled_unlinked = sorted(set(unlinked))
        if filled_unlinked:
            self._blocked_reason = "acknowledged fill has no durable trade snapshot"
            self._blocked_client_order_id = filled_unlinked[0]
        else:
            self._blocked_reason = None
            self._blocked_client_order_id = None
        return {
            "pending": len(pending),
            "filled_unlinked": filled_unlinked,
            "terminal_unfilled": terminal_unfilled,
        }

    async def _recover_ambiguous(self, key: str, request: OrderRequest) -> OrderResult:
        lookup = getattr(self._inner, "get_order_by_client_id", None)
        if lookup is None:
            raise AmbiguousOrderSubmissionError(
                f"order {key} has a durable intent but executor cannot query by client id"
            )
        status = await lookup(request.symbol, key)
        if not status:
            raise AmbiguousOrderSubmissionError(
                f"order {key} remains ambiguous: venue lookup returned no authoritative result; "
                "automatic resubmission blocked"
            )
        if not isinstance(status, Mapping):
            raise AmbiguousOrderSubmissionError(
                f"order {key} lookup returned malformed evidence; manual reconciliation required"
            )
        venue_state = str(status.get("status", "")).upper()
        terminal_states = {
            "FILLED",
            "PARTIALLY_FILLED",
            "CANCELED",
            "EXPIRED",
            "REJECTED",
        }
        if venue_state not in terminal_states:
            raise AmbiguousOrderSubmissionError(
                f"order {key} is still {venue_state or 'unknown'}; automatic resubmission blocked"
            )

        # Recovery bypasses the normal Binance submit-response validator. Bind
        # the lookup result back to the exact intent before it can be persisted
        # as acknowledged exposure.
        expected_side = (
            ("SELL" if request.side == TradeSide.LONG else "BUY")
            if request.reduce_only
            else ("BUY" if request.side == TradeSide.LONG else "SELL")
        )
        if (
            status.get("symbol") != request.symbol
            or status.get("clientOrderId", status.get("client_order_id")) != key
            or str(status.get("side", "")).upper() != expected_side
        ):
            raise AmbiguousOrderSubmissionError(
                f"order {key} lookup identity does not match the durable request"
            )
        try:
            filled = float(
                status.get("executedQty", status.get("filled_quantity", 0)) or 0
            )
            price = float(status.get("avgPrice", status.get("fill_price", 0)) or 0)
        except (TypeError, ValueError, OverflowError):
            raise AmbiguousOrderSubmissionError(
                f"order {key} lookup returned invalid fill economics"
            ) from None
        order_id = str(status.get("orderId", status.get("order_id", "")))
        if (
            not math.isfinite(filled)
            or not math.isfinite(price)
            or filled < 0
            or filled > request.quantity * (1 + 1e-9)
            or price < 0
            or (filled > 0 and (price <= 0 or not order_id))
            or (filled == 0 and price != 0)
        ):
            raise AmbiguousOrderSubmissionError(
                f"order {key} lookup returned invalid or out-of-bounds fill evidence"
            )
        accepted = filled > 0
        result = OrderResult(
            order_id=order_id,
            symbol=request.symbol,
            side=request.side,
            filled_quantity=filled,
            fill_price=price,
            filled_at=str(
                status.get("updateTime")
                or status.get("filled_at")
                or datetime.now(timezone.utc).isoformat()
            ),
            success=accepted,
            error=(None if accepted else f"venue order terminal state: {venue_state}"),
        )
        await self._state_store.save_order_result(
            key, request.symbol, self._result_payload(result)
        )
        return result

    @staticmethod
    def _request_payload(request: OrderRequest) -> dict[str, Any]:
        return {
            "symbol": request.symbol,
            "side": request.side.value,
            "quantity": request.quantity,
            "reference_price": request.reference_price,
            "order_type": request.order_type,
            "client_order_id": request.client_order_id,
            "reduce_only": request.reduce_only,
        }

    @staticmethod
    def _result_payload(result: OrderResult) -> dict[str, Any]:
        return {
            "order_id": result.order_id,
            "symbol": result.symbol,
            "side": result.side.value,
            "filled_quantity": result.filled_quantity,
            "fill_price": result.fill_price,
            "filled_at": result.filled_at,
            "success": result.success,
            "error": result.error,
        }

    @staticmethod
    def _result_from_payload(
        payload: dict[str, Any], request: OrderRequest
    ) -> OrderResult:
        return OrderResult(
            order_id=str(payload.get("order_id", "")),
            symbol=str(payload.get("symbol") or request.symbol),
            side=TradeSide(str(payload.get("side") or request.side.value)),
            filled_quantity=float(payload.get("filled_quantity", 0) or 0),
            fill_price=float(payload.get("fill_price", 0) or 0),
            filled_at=str(
                payload.get("filled_at") or datetime.now(timezone.utc).isoformat()
            ),
            success=bool(payload.get("success")),
            error=payload.get("error"),
        )

    def _key(self, request: OrderRequest) -> str:
        raw = "|".join(
            [
                request.symbol,
                request.side.value,
                float(request.quantity).hex(),
                float(request.reference_price).hex(),
                request.order_type,
                str(request.reduce_only),
            ]
        )
        return "aitos-" + hashlib.sha256(raw.encode()).hexdigest()[:24]

    async def get_order_status(self, symbol: str, order_id: str) -> dict | None:
        return await self._inner.get_order_status(symbol, order_id)

    async def get_order_by_client_id(
        self, symbol: str, client_order_id: str
    ) -> dict | None:
        return await self._inner.get_order_by_client_id(symbol, client_order_id)

    async def place_stop_loss_order(self, *args: Any, **kwargs: Any) -> OrderResult:
        return await self._inner.place_stop_loss_order(*args, **kwargs)

    async def place_take_profit_order(self, *args: Any, **kwargs: Any) -> OrderResult:
        return await self._inner.place_take_profit_order(*args, **kwargs)

    async def cancel_resting_order(self, *args: Any, **kwargs: Any) -> None:
        await self._inner.cancel_resting_order(*args, **kwargs)

    async def get_resting_order_status(self, *args: Any, **kwargs: Any) -> str | None:
        return await self._inner.get_resting_order_status(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
