"""Fail-closed comparison of local lifecycle quantities with venue snapshots."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from aitos.models.trade import TradeSide


def assert_position_snapshot_matches(
    trades: Iterable[Any], rows: Any, *, hedge_mode: bool
) -> None:
    """Check the entire account, not just configured scanner symbols.

    This is a point-in-time read-only gate, not protection/HA certification.
    Unknown manual positions and opposite-side offsets cannot disappear in a net sum.
    """
    if not isinstance(rows, list):
        raise ValueError("venue position snapshot must be a list")
    expected: dict[tuple[str, str], float] = {}
    for trade in trades:
        quantity = float(trade.quantity)
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("local position has invalid quantity")
        side = (
            trade.side.value if isinstance(trade.side, TradeSide) else str(trade.side)
        )
        if side not in {"LONG", "SHORT"} or not trade.symbol:
            raise ValueError("invalid local position identity")
        key = (trade.symbol, side)
        expected[key] = expected.get(key, 0) + quantity
    actual: dict[tuple[str, str], float] = {}
    identities: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid venue position row")
        try:
            if isinstance(row.get("positionAmt"), bool):
                raise ValueError("boolean position quantity")
            quantity = float(row["positionAmt"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError("invalid venue position quantity") from None
        symbol = row.get("symbol")
        side = row.get("positionSide")
        if not math.isfinite(quantity) or not isinstance(symbol, str) or not symbol:
            raise ValueError("invalid venue position identity/quantity")
        if hedge_mode:
            if side not in {"LONG", "SHORT"}:
                raise ValueError("hedge position side missing or inconsistent")
            if (side == "LONG" and quantity < 0) or (side == "SHORT" and quantity > 0):
                raise ValueError("hedge position amount has inconsistent sign")
            key = (symbol, side)
        else:
            if side != "BOTH":
                raise ValueError("one-way snapshot requires BOTH position side")
            key = (symbol, "LONG" if quantity >= 0 else "SHORT")
        identity = (symbol, side)
        if identity in identities:
            raise ValueError("duplicate venue position row")
        identities.add(identity)
        if quantity:
            actual[key] = abs(quantity)
    if expected.keys() != actual.keys() or any(
        not math.isclose(value, actual[key], rel_tol=1e-9, abs_tol=1e-12)
        for key, value in expected.items()
    ):
        raise ValueError("local/venue exposure mismatch; reconciliation required")
