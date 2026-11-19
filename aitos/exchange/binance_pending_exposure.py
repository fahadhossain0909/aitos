"""Strict, side-aware projection of Binance USD-M open entry orders.

This is a pure adapter: callers must supply a complete authenticated open-order
snapshot plus fresh mark prices, validated protective stop prices and leverage.
Missing/stale inputs fail closed. Reduce-only and hedge-mode closing orders are
never counted as new entry exposure. No exchange IO or order mutation occurs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any

from aitos.risk.models import PendingOrderExposure, PortfolioState

OPEN_STATUSES = {"NEW", "PARTIALLY_FILLED"}
SUPPORTED_ORDER_TYPES = {
    "LIMIT",
    "MARKET",
    "STOP",
    "STOP_MARKET",
    "TAKE_PROFIT",
    "TAKE_PROFIT_MARKET",
    "TRAILING_STOP_MARKET",
}


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite positive decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} must be a finite positive decimal") from None
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field} must be a finite positive decimal")
    return result


def _flag(value: Any, field: str) -> bool:
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise ValueError(f"{field} must be a boolean exchange field")


def pending_exposures_from_open_orders(
    orders: list[dict[str, Any]],
    *,
    mark_prices: Mapping[str, Any],
    stop_prices: Mapping[tuple[str, str], Any],
    leverage_by_symbol: Mapping[str, Any],
    orders_observed_at: datetime,
    risk_inputs_observed_at: datetime,
    now: datetime | None = None,
    max_age_seconds: float = 5.0,
    max_snapshot_skew_seconds: float = 2.0,
) -> tuple[PendingOrderExposure, ...]:
    """Convert a complete Binance ``openOrders`` response into risk exposures.

    Risk is remaining base quantity multiplied by distance from the supplied
    mark to a valid thesis stop. This is suitable for linear USD-M contracts;
    inverse/coin-margined instruments require a different adapter.
    """
    if not isinstance(orders, list) or len(orders) > 10000:
        raise ValueError("complete bounded Binance open-order list required")
    current = now or datetime.now(timezone.utc)
    if (
        current.utcoffset() is None
        or not isinstance(orders_observed_at, datetime)
        or not isinstance(risk_inputs_observed_at, datetime)
        or orders_observed_at.utcoffset() is None
        or risk_inputs_observed_at.utcoffset() is None
        or not isfinite(max_age_seconds)
        or not 0 < max_age_seconds <= 30
        or not isfinite(max_snapshot_skew_seconds)
        or not 0 <= max_snapshot_skew_seconds <= 10
    ):
        raise ValueError("aware timestamps and bounded freshness limits required")
    order_age = (current - orders_observed_at).total_seconds()
    input_age = (current - risk_inputs_observed_at).total_seconds()
    snapshot_skew = abs((orders_observed_at - risk_inputs_observed_at).total_seconds())
    if (
        not 0 <= order_age <= max_age_seconds
        or not 0 <= input_age <= max_age_seconds
        or snapshot_skew > max_snapshot_skew_seconds
    ):
        raise ValueError("stale or incoherent pending-order risk snapshot")
    if not all(
        isinstance(value, Mapping)
        for value in (mark_prices, stop_prices, leverage_by_symbol)
    ):
        raise ValueError("fresh mark, stop and leverage mappings are required")

    result: list[PendingOrderExposure] = []
    seen_ids: set[str] = set()
    for row in orders:
        if not isinstance(row, dict):
            raise ValueError("malformed Binance open-order row")
        raw_id = row.get("orderId")
        if isinstance(raw_id, bool) or not str(raw_id or "").isdigit():
            raise ValueError("positive Binance order identity required")
        order_id = str(int(raw_id))
        if int(order_id) <= 0 or order_id in seen_ids:
            raise ValueError("duplicate or invalid Binance order identity")
        seen_ids.add(order_id)

        symbol = row.get("symbol")
        side = row.get("side")
        position_side = row.get("positionSide")
        order_type = row.get("type")
        status = row.get("status")
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol != symbol.strip().upper()
            or side not in {"BUY", "SELL"}
            or position_side not in {"BOTH", "LONG", "SHORT"}
            or order_type not in SUPPORTED_ORDER_TYPES
            or status not in OPEN_STATUSES
        ):
            raise ValueError("unrecognized or non-open Binance order contract")

        reduce_only = _flag(row.get("reduceOnly", False), "reduceOnly")
        close_position = _flag(row.get("closePosition", False), "closePosition")
        # In hedge mode, the side opposite the named position leg is a close,
        # even though Binance does not accept reduceOnly=true for that mode.
        hedge_close = (position_side == "LONG" and side == "SELL") or (
            position_side == "SHORT" and side == "BUY"
        )
        if reduce_only or close_position or hedge_close:
            continue

        direction = (
            "LONG"
            if position_side == "LONG" or (position_side == "BOTH" and side == "BUY")
            else "SHORT"
        )
        try:
            original = _decimal(row.get("origQty"), "origQty")
            executed_raw = row.get("executedQty", "0")
            if isinstance(executed_raw, bool):
                raise ValueError("executedQty must be a non-negative decimal")
            executed = Decimal(str(executed_raw))
        except (InvalidOperation, ValueError):
            raise ValueError("invalid Binance order quantity evidence") from None
        if not executed.is_finite() or executed < 0 or executed > original:
            raise ValueError("invalid Binance executed quantity")
        remaining = original - executed
        if remaining <= 0:
            # An open-order endpoint must not contain fully executed rows.
            raise ValueError("open order has no remaining quantity")

        try:
            mark = _decimal(mark_prices[symbol], f"mark price for {symbol}")
            stop = _decimal(stop_prices[(symbol, direction)], f"stop for {symbol}")
            leverage = _decimal(leverage_by_symbol[symbol], f"leverage for {symbol}")
        except KeyError as exc:
            raise ValueError(
                f"missing authoritative risk input: {exc.args[0]}"
            ) from None
        if (direction == "LONG" and stop >= mark) or (
            direction == "SHORT" and stop <= mark
        ):
            raise ValueError("protective stop is on the wrong side of mark price")

        notional = remaining * mark
        risk = remaining * abs(mark - stop)
        notional_float, risk_float, leverage_float = (
            float(notional),
            float(risk),
            float(leverage),
        )
        if not all(
            isfinite(value) for value in (notional_float, risk_float, leverage_float)
        ):
            raise ValueError("projected pending exposure exceeds finite numeric range")
        result.append(
            PendingOrderExposure(
                order_id=f"binance:{order_id}",
                symbol=symbol,
                side=direction,
                remaining_notional_usd=notional_float,
                risk_amount_usd=risk_float,
                leverage=leverage_float,
            )
        )
    return tuple(result)


def attach_pending_exposures(
    portfolio: PortfolioState,
    orders: list[dict[str, Any]],
    *,
    mark_prices: Mapping[str, Any],
    stop_prices: Mapping[tuple[str, str], Any],
    leverage_by_symbol: Mapping[str, Any],
    orders_observed_at: datetime,
    risk_inputs_observed_at: datetime,
    now: datetime | None = None,
    max_age_seconds: float = 5.0,
    max_snapshot_skew_seconds: float = 2.0,
) -> PortfolioState:
    """Return a new portfolio snapshot with validated pending entry exposure."""
    if not isinstance(portfolio, PortfolioState):
        raise TypeError("PortfolioState required")
    pending = pending_exposures_from_open_orders(
        orders,
        mark_prices=mark_prices,
        stop_prices=stop_prices,
        leverage_by_symbol=leverage_by_symbol,
        orders_observed_at=orders_observed_at,
        risk_inputs_observed_at=risk_inputs_observed_at,
        now=now,
        max_age_seconds=max_age_seconds,
        max_snapshot_skew_seconds=max_snapshot_skew_seconds,
    )
    return replace(portfolio, pending_orders=pending)
