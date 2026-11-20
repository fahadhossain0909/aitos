"""Fail-closed cumulative-fill reconciliation for triggered protective algos.

This module deliberately does not mutate trades or submit/cancel orders. It
validates the venue child-order snapshot and calculates the unapplied delta.
A caller must persist the resulting trade mutation before advancing its
durable applied-cumulative watermark.
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
    status: str
    cumulative_quantity: Decimal
    previously_applied_quantity: Decimal
    delta_quantity: Decimal
    average_price: Decimal

    @property
    def has_new_fill(self) -> bool:
        return self.delta_quantity > 0


def _positive_decimal(value: Any, field: str, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ProtectionChildReconciliationError(f"{field} must be numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ProtectionChildReconciliationError(f"{field} must be numeric") from None
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        raise ProtectionChildReconciliationError(f"{field} must be finite and positive")
    return parsed


def calculate_protection_child_fill_delta(
    algo: dict[str, Any],
    child: dict[str, Any],
    *,
    expected_symbol: str,
    expected_side: str,
    expected_algo_id: str,
    previously_applied_quantity: Any,
) -> ProtectionChildFillDelta:
    """Validate one authenticated child snapshot and calculate its new fill.

    Binance child order quantities are cumulative. This function never treats
    an order's status alone as proof of a fill and rejects quantity regression.
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
    if algo.get("symbol") != expected_symbol or child.get("symbol") != expected_symbol:
        raise ProtectionChildReconciliationError("child symbol mismatch")
    if child.get("side") != expected_side:
        raise ProtectionChildReconciliationError("child side mismatch")
    if (
        algo.get("actualOrderId") is not None
        and str(algo.get("actualOrderId")) != child_id
    ):
        raise ProtectionChildReconciliationError("algo-to-child linkage mismatch")

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

    cumulative = _positive_decimal(
        child.get("executedQty", "0"), "executedQty", allow_zero=True
    )
    applied = _positive_decimal(
        previously_applied_quantity,
        "previously_applied_quantity",
        allow_zero=True,
    )
    if cumulative < applied:
        raise ProtectionChildReconciliationError("cumulative child fill regressed")
    if status == "FILLED":
        original = _positive_decimal(child.get("origQty"), "origQty")
        if cumulative != original:
            raise ProtectionChildReconciliationError(
                "FILLED child quantity does not equal original quantity"
            )
    average = (
        _positive_decimal(child.get("avgPrice"), "avgPrice")
        if cumulative > 0
        else Decimal("0")
    )
    return ProtectionChildFillDelta(
        algo_id=algo_id,
        child_order_id=child_id,
        status=status,
        cumulative_quantity=cumulative,
        previously_applied_quantity=applied,
        delta_quantity=cumulative - applied,
        average_price=average,
    )
