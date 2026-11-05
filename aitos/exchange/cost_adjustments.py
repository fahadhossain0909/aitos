"""Safe publication of Binance account-income records into trade lifecycle."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from aitos.core.contracts import Event

TradeResolver = Callable[[dict[str, Any]], Awaitable[str | None]]


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

            trade_id = await self._resolve_trade_id(row)
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
