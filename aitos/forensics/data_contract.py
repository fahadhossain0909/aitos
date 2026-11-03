"""Data contract checkpoints between pipeline stages.

Validates payload shape and separates *late* data from *corrupted* data so
error counters stay diagnostically useful.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aitos.forensics.data_flow_schema import DataIssueClass


@dataclass(frozen=True)
class ContractResult:
    ok: bool
    issue: DataIssueClass = DataIssueClass.NONE
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issue": self.issue.value,
            "detail": self.detail,
        }


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def check_trade_payload(
    payload: dict[str, Any],
    *,
    now_ms: float | None = None,
    stale_after_ms: float = 15_000.0,
) -> ContractResult:
    """Validate a trade-like dict (TradeTick wire or Binance aggTrade)."""
    if not isinstance(payload, dict):
        return ContractResult(False, DataIssueClass.SCHEMA_INVALID, "payload_not_dict")

    symbol = payload.get("symbol") or payload.get("s")
    if not symbol:
        return ContractResult(False, DataIssueClass.SCHEMA_INVALID, "missing_symbol")

    price = _num(payload.get("price", payload.get("p")))
    qty = _num(payload.get("quantity", payload.get("q")))
    if price is None or price <= 0:
        return ContractResult(False, DataIssueClass.SEMANTIC_INVALID, "invalid_price")
    if qty is None or qty < 0:
        return ContractResult(
            False, DataIssueClass.SEMANTIC_INVALID, "invalid_quantity"
        )

    event_ms = _num(payload.get("T", payload.get("E", payload.get("timestamp_ms"))))
    if event_ms is not None and now_ms is not None:
        age = max(0.0, float(now_ms) - float(event_ms))
        if age >= stale_after_ms:
            return ContractResult(
                True,
                DataIssueClass.STALE,
                f"source_age_ms={round(age, 1)}",
            )

    return ContractResult(True, DataIssueClass.NONE, "ok")


def check_orderbook_payload(payload: dict[str, Any]) -> ContractResult:
    if not isinstance(payload, dict):
        return ContractResult(False, DataIssueClass.SCHEMA_INVALID, "payload_not_dict")
    symbol = payload.get("symbol") or payload.get("s")
    if not symbol:
        return ContractResult(False, DataIssueClass.SCHEMA_INVALID, "missing_symbol")
    bids = payload.get("bids") if "bids" in payload else payload.get("b")
    asks = payload.get("asks") if "asks" in payload else payload.get("a")
    if bids is None and asks is None:
        return ContractResult(
            False, DataIssueClass.SCHEMA_INVALID, "missing_book_sides"
        )
    return ContractResult(True, DataIssueClass.NONE, "ok")


__all__ = [
    "ContractResult",
    "check_orderbook_payload",
    "check_trade_payload",
]
