"""Fail-closed cumulative-fill reconciliation for triggered protective algos.

This module deliberately does not mutate trades or submit/cancel orders. It
validates authenticated venue algo/child snapshots and calculates the unapplied
quantity and quote deltas. A caller must persist both deltas with its trade
snapshot before acknowledging the corresponding durable journal evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


class ProtectionChildReconciliationError(ValueError):
    """Venue child evidence is incomplete, inconsistent, or non-monotonic."""


@dataclass(frozen=True)
class ProtectionChildFillDelta:
    algo_id: str
    child_order_id: str
    order_type: str
    status: str
    cumulative_quantity: Decimal
    previously_applied_quantity: Decimal
    delta_quantity: Decimal
    cumulative_quote: Decimal
    previously_applied_quote: Decimal
    delta_quote: Decimal
    average_price: Decimal

    @property
    def has_new_fill(self) -> bool:
        return self.delta_quantity > 0


def _decimal_value(value: Any, field: str, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ProtectionChildReconciliationError(f"{field} must be numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ProtectionChildReconciliationError(f"{field} must be numeric") from None
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ProtectionChildReconciliationError(
            f"{field} must be finite and {qualifier}"
        )
    return parsed


def calculate_protection_child_fill_delta(
    algo: dict[str, Any],
    child: dict[str, Any],
    *,
    expected_symbol: str,
    expected_side: str,
    expected_algo_id: str,
    previously_applied_quantity: Any,
    previously_applied_quote: Any = "0",
    expected_position_side: str | None = None,
) -> ProtectionChildFillDelta:
    """Validate one authenticated triggered-algo child and calculate new fills.

    Venue executedQty/cumQuote are cumulative. The incremental average fill
    price is therefore delta cumQuote / delta executedQty, never the venue's
    lifetime avgPrice applied to only the latest quantity delta.
    """
    if not isinstance(algo, dict) or not isinstance(child, dict):
        raise ProtectionChildReconciliationError(
            "algo and child snapshots are required"
        )
    algo_id = str(algo.get("algoId", ""))
    child_id = str(child.get("orderId", ""))
    if not expected_algo_id or algo_id != expected_algo_id:
        raise ProtectionChildReconciliationError("protective algo identity mismatch")
    if not child_id or child_id in {"0", "None"}:
        raise ProtectionChildReconciliationError(
            "triggered child order identity missing"
        )
    if str(algo.get("actualOrderId", "")) != child_id:
        raise ProtectionChildReconciliationError("algo-to-child linkage mismatch")
    if algo.get("symbol") != expected_symbol or child.get("symbol") != expected_symbol:
        raise ProtectionChildReconciliationError("child symbol mismatch")
    if algo.get("side") != expected_side or child.get("side") != expected_side:
        raise ProtectionChildReconciliationError("child side mismatch")

    order_type = str(algo.get("orderType", ""))
    if order_type not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
        raise ProtectionChildReconciliationError("unsupported protective algo type")
    if algo.get("algoType") != "CONDITIONAL":
        raise ProtectionChildReconciliationError("algo is not a conditional order")
    if algo.get("algoStatus") not in {"TRIGGERED", "FINISHED"}:
        raise ProtectionChildReconciliationError("algo is not confirmed triggered")
    if algo.get("closePosition") not in (False, "false"):
        raise ProtectionChildReconciliationError("close-position algo is unsupported")
    if expected_position_side is not None:
        if algo.get("positionSide") != expected_position_side:
            raise ProtectionChildReconciliationError("algo position-side mismatch")
        if child.get("positionSide") != expected_position_side:
            raise ProtectionChildReconciliationError("child position-side mismatch")

    status = str(child.get("status", "")).upper()
    allowed_statuses = {
        "NEW",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "EXPIRED",
        "EXPIRED_IN_MATCH",
        "REJECTED",
    }
    if status not in allowed_statuses:
        raise ProtectionChildReconciliationError("unsupported or missing child status")

    cumulative = _decimal_value(
        child.get("executedQty", "0"), "executedQty", allow_zero=True
    )
    original = _decimal_value(child.get("origQty"), "origQty")
    if cumulative > original:
        raise ProtectionChildReconciliationError(
            "executed child quantity exceeds original quantity"
        )
    applied = _decimal_value(
        previously_applied_quantity,
        "previously_applied_quantity",
        allow_zero=True,
    )
    if cumulative < applied:
        raise ProtectionChildReconciliationError("cumulative child fill regressed")
    if status == "FILLED" and cumulative != original:
        raise ProtectionChildReconciliationError(
            "FILLED child quantity does not equal original quantity"
        )

    cumulative_quote = _decimal_value(
        child.get("cumQuote", "0"), "cumQuote", allow_zero=True
    )
    applied_quote = _decimal_value(
        previously_applied_quote,
        "previously_applied_quote",
        allow_zero=True,
    )
    if cumulative_quote < applied_quote:
        raise ProtectionChildReconciliationError("cumulative child quote regressed")
    if cumulative == 0 and cumulative_quote != 0:
        raise ProtectionChildReconciliationError(
            "quote exists without executed quantity"
        )
    delta_quantity = cumulative - applied
    delta_quote = cumulative_quote - applied_quote
    if delta_quantity == 0 and delta_quote != 0:
        raise ProtectionChildReconciliationError(
            "quote changed without new executed quantity"
        )
    if delta_quantity > 0 and delta_quote <= 0:
        raise ProtectionChildReconciliationError(
            "new fill has no positive incremental quote value"
        )
    average = delta_quote / delta_quantity if delta_quantity > 0 else Decimal(0)
    return ProtectionChildFillDelta(
        algo_id=algo_id,
        child_order_id=child_id,
        order_type=order_type,
        status=status,
        cumulative_quantity=cumulative,
        previously_applied_quantity=applied,
        delta_quantity=delta_quantity,
        cumulative_quote=cumulative_quote,
        previously_applied_quote=applied_quote,
        delta_quote=delta_quote,
        average_price=average,
    )
