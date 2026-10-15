"""Structured, low-overhead telemetry for the open-position lifecycle.

Telemetry is observational: failures are swallowed and never alter trading decisions.
Events are correlated by trade_id/position_id and use bounded, structured fields.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from aitos.logging_setup import get_logger

logger = get_logger("aitos.telemetry.position_lifecycle")


def _value(obj: Any, name: str, default: Any = None) -> Any:
    value = getattr(obj, name, default)
    return getattr(value, "value", value)


def position_snapshot(trade: Any) -> dict[str, Any]:
    """Return a conservative, JSON-friendly snapshot without mutating a trade."""
    if trade is None:
        return {}
    fields = (
        "trade_id", "symbol", "strategy_id", "side", "state", "entry_price",
        "last_marked_price", "quantity", "remaining_quantity", "sl_price",
        "tp_price", "entry_time", "exit_time", "exit_reason",
    )
    snapshot: dict[str, Any] = {}
    for name in fields:
        value = _value(trade, name)
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            snapshot[name] = value
        else:
            snapshot[name] = str(value)
    trade_id = snapshot.get("trade_id")
    if trade_id:
        snapshot["position_id"] = str(trade_id)
        snapshot["trace_id"] = f"position:{trade_id}"
    return snapshot


def emit_position_event(event: str, trade: Any = None, **fields: Any) -> None:
    """Emit a structured event; telemetry must never break trading."""
    try:
        payload = {
            "event": event,
            "event_id": uuid4().hex,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **position_snapshot(trade),
        }
        # Prevent caller-provided fields from overriding correlation metadata.
        for key, value in fields.items():
            if key in {"event", "event_id", "timestamp", "position_id", "trace_id"}:
                continue
            if value is None or isinstance(value, (str, int, float, bool)):
                payload[key] = value
            elif isinstance(value, (tuple, list)):
                payload[key] = [str(item) for item in value[:32]]
            elif isinstance(value, dict):
                payload[key] = {
                    str(k): v for k, v in list(value.items())[:32]
                    if v is None or isinstance(v, (str, int, float, bool))
                }
            else:
                payload[key] = str(value)
        logger.info(
            "position lifecycle telemetry",
            extra={"aitos_extra": payload},
        )
    except Exception:
        # Instrumentation is explicitly non-authoritative.
        return
