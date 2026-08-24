"""Durable runtime state for production trading recovery and risk tracking."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from aitos.app import LivePortfolioTracker
from aitos.eventbus.redis_bus import EventBus, Subscription
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


class DurableTradingStateStore:
    """Small ClickHouse-backed state store used by live trading recovery."""

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    async def initialize(self) -> None:
        if self._repository is None or self._repository._client is None:
            return
        await self._repository._client.command(CREATE_RUNTIME_STATE)
        await self._repository._client.command(CREATE_DRAWDOWN_STATE)

    async def save_trade(self, trade: Trade) -> None:
        client = self._client()
        payload = trade.to_dict()
        await client.insert(
            "trade_runtime_state",
            [[
                trade.trade_id,
                trade.symbol,
                trade.state.value,
                json.dumps(payload, sort_keys=True, default=str),
                datetime.now(timezone.utc),
            ]],
            column_names=[
                "trade_id",
                "symbol",
                "state",
                "payload_json",
                "updated_at",
            ],
        )

    async def delete_trade(self, trade_id: str) -> None:
        client = self._client()
        await client.command(
            "ALTER TABLE trade_runtime_state DELETE "
            "WHERE trade_id = {trade_id:String}",
            parameters={"trade_id": trade_id},
        )

    async def load_open_trades(self) -> List[Trade]:
        client = self._client()
        result = await client.query(
            """
            SELECT trade_id, argMax(payload_json, updated_at) AS payload_json
            FROM trade_runtime_state
            WHERE state IN ('position_opened', 'exit_triggered')
            GROUP BY trade_id
            """
        )
        trades: List[Trade] = []
        for row in result.result_rows:
            try:
                payload = json.loads(row[1])
                trades.append(_trade_from_dict(payload))
            except Exception as exc:
                logger.error(
                    "failed to restore trade state",
                    extra={"aitos_extra": {"error": str(exc)}},
                )
        return trades

    async def save_drawdown(self, asset: str, equity: float, peak: float) -> None:
        client = self._client()
        drawdown = ((peak - equity) / peak * 100.0) if peak > 0 else 0.0
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

    async def load_peak_equity(self, asset: str) -> Optional[float]:
        client = self._client()
        result = await client.query(
            """
            SELECT argMax(peak_equity_usd, time) AS peak_equity_usd
            FROM portfolio_drawdown_state
            WHERE asset = {asset:String}
            """,
            parameters={"asset": asset},
        )
        if not result.result_rows or result.result_rows[0][0] is None:
            return None
        return float(result.result_rows[0][0])

    def _client(self) -> Any:
        if self._repository is None or self._repository._client is None:
            raise RuntimeError(
                "durable trading state requires an initialized ClickHouse repository"
            )
        return self._repository._client


def _trade_from_dict(data: Dict[str, Any]) -> Trade:
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
        rejection_reason=data.get("rejection_reason"),
        updated_at=str(data.get("updated_at") or data["entry_time"]),
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
        self._subscriptions: List[Subscription] = []

    async def restore(self) -> int:
        trades = await self._store.load_open_trades()
        for trade in trades:
            self._lifecycle._open_trades[trade.trade_id] = trade
        if trades:
            logger.warning(
                "restored open trades after process restart",
                extra={"aitos_extra": {"count": len(trades)}},
            )
        return len(trades)

    async def initialize(self) -> None:
        await self._store.initialize()
        self._subscriptions = [
            await self._event_bus.subscribe(
                "trade.position_opened",
                self._save_event,
                group="trade-state-persistence",
            ),
            await self._event_bus.subscribe(
                "trade.position_updated",
                self._save_event,
                group="trade-state-persistence",
            ),
            await self._event_bus.subscribe(
                "trade.trailing_sl",
                self._save_event,
                group="trade-state-persistence",
            ),
            await self._event_bus.subscribe(
                "trade.partial_close",
                self._save_event,
                group="trade-state-persistence",
            ),
            await self._event_bus.subscribe(
                "trade.position_closed",
                self._delete_event,
                group="trade-state-persistence",
            ),
        ]

    async def shutdown(self) -> None:
        for subscription in self._subscriptions:
            subscription.cancel()
        self._subscriptions.clear()

    async def _save_event(self, event: Any) -> None:
        try:
            payload = dict(event.payload)
            trade = _trade_from_dict(payload)
            if trade.state in (
                TradeLifecycleState.POSITION_OPENED,
                TradeLifecycleState.EXIT_TRIGGERED,
            ):
                await self._store.save_trade(trade)
        except Exception as exc:
            logger.error(
                "trade state persistence failed",
                extra={"aitos_extra": {"error": str(exc)}},
            )

    async def _delete_event(self, event: Any) -> None:
        trade_id = event.payload.get("trade_id")
        if trade_id:
            await self._store.delete_trade(str(trade_id))


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
        self._peak_equity_usd = await self._state_store.load_peak_equity(self._asset)

    async def refresh_equity(self) -> float:
        equity = await super().refresh_equity()
        await self._state_store.save_drawdown(
            self._asset,
            equity,
            self._peak_equity_usd or equity,
        )
        return equity


class IdempotentOrderExecutor(OrderExecutor):
    """Adds deterministic client IDs and in-process deduplication to an executor."""

    def __init__(self, inner: OrderExecutor) -> None:
        self._inner = inner
        self._completed: Dict[str, OrderResult] = {}
        self._inflight: Dict[str, asyncio.Future[OrderResult]] = {}

    @property
    def supports_exchange_side_stops(self) -> bool:
        return self._inner.supports_exchange_side_stops

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        key = request.client_order_id or self._key(request)
        request = (
            request
            if request.client_order_id
            else replace(request, client_order_id=key)
        )
        if key in self._completed:
            return self._completed[key]
        if key in self._inflight:
            return await self._inflight[key]
        task = asyncio.create_task(self._inner.submit_order(request))
        self._inflight[key] = task
        try:
            result = await task
            if result.success:
                self._completed[key] = result
            return result
        finally:
            self._inflight.pop(key, None)

    def _key(self, request: OrderRequest) -> str:
        raw = "|".join(
            [
                request.symbol,
                request.side.value,
                f"{request.quantity:.12g}",
                f"{request.reference_price:.12g}",
                request.order_type,
            ]
        )
        return "aitos-" + hashlib.sha256(raw.encode()).hexdigest()[:24]

    async def place_stop_loss_order(
        self, *args: Any, **kwargs: Any
    ) -> OrderResult:
        return await self._inner.place_stop_loss_order(*args, **kwargs)

    async def place_take_profit_order(
        self, *args: Any, **kwargs: Any
    ) -> OrderResult:
        return await self._inner.place_take_profit_order(*args, **kwargs)

    async def cancel_resting_order(self, *args: Any, **kwargs: Any) -> None:
        await self._inner.cancel_resting_order(*args, **kwargs)

    async def get_resting_order_status(
        self, *args: Any, **kwargs: Any
    ) -> Optional[str]:
        return await self._inner.get_resting_order_status(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
