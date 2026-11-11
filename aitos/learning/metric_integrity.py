"""Shared fail-closed numerical contracts for research validation evidence."""

from __future__ import annotations

from math import isfinite
from numbers import Integral, Real
from typing import Any


def validate_count(value: Any, name: str, *, minimum: int = 0) -> None:
    """Counts/durations are integers, never booleans, fractions, NaN or infinity."""
    if isinstance(value, bool) or not isinstance(value, Integral) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def metric_integrity_error(metrics: Any) -> str | None:
    """Report malformed metrics; do not conflate missing data with zero performance.

    Sharpe remains the existing engine metric, not an annualization/significance claim.
    Returns and drawdowns are not capped here: leveraged loss may exceed equity.
    """
    for name in ("total_return", "max_drawdown", "sharpe", "trades"):
        raw = getattr(metrics, name, None)
        if isinstance(raw, bool) or not isinstance(raw, Real):
            return f"{name} must be finite numeric evidence"
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            return f"{name} must be finite numeric evidence"
        if not isfinite(value):
            return f"{name} must be finite numeric evidence"
        if name == "max_drawdown" and value < 0:
            return "max_drawdown cannot be negative"
        if name == "trades" and (value < 0 or not value.is_integer()):
            return "trades must be a non-negative integer"
    return None
