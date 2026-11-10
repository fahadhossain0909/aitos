"""Explicitly injected protection-evidence monitor; no orders or recovery writes.

The capture callable supplies scoped journal intents and a complete inventory
export. This adapter does not authenticate export provenance or open exchanges.
Matching rows remain reconciliation candidates, never protection guarantees.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone

from aitos.execution.protection_audit import audit_protection_inventory
from aitos.trading.account_reconciliation import name

STATUSES = {
    "unowned_order",
    "mismatch",
    "not_in_open_inventory",
    "unapplied_match",
    "applied_order_present",
}


class ProtectionObservationUnavailable(RuntimeError):
    pass


class ExportedProtectionObserver:
    def __init__(self, capture, *, scope):
        if not callable(capture):
            raise TypeError("explicit read-only capture required")
        self._capture = capture
        self._scope = name(scope)
        self._lock = asyncio.Lock()

    async def observe(self):
        if self._lock.locked():
            raise ProtectionObservationUnavailable(
                "protection observation already pending"
            )
        async with self._lock:
            try:
                capture = await asyncio.wait_for(self._capture(), timeout=8)
                if (
                    type(capture) is not dict
                    or set(capture) != {"intents", "evidence"}
                    or type(capture["intents"]) is not list
                    or type(capture["evidence"]) is not dict
                ):
                    raise ValueError("recognized protection capture required")
                evidence = capture["evidence"]
                if (
                    len(capture["intents"]) > 10000
                    or type(evidence.get("orders")) is not list
                    or len(evidence["orders"]) > 10000
                    or type(evidence.get("symbols")) is not list
                    or len(evidence["symbols"]) > 2000
                    or len(json.dumps(capture, allow_nan=False)) > 4 * 1024 * 1024
                ):
                    raise ValueError("bounded protection capture required")
                for row in capture["intents"]:
                    if (
                        not isinstance(row, dict)
                        or not isinstance(row.get("client_id"), str)
                        or not 1 <= len(row["client_id"]) <= 128
                    ):
                        raise ValueError("bounded journal identity required")
                result = audit_protection_inventory(
                    capture["intents"], capture["evidence"], scope=self._scope
                )
                findings, counts = [], {status: 0 for status in sorted(STATUSES)}
                for row in result["findings"]:
                    symbol, status = row["symbol"], row["status"]
                    if (
                        not isinstance(symbol, str)
                        or not re.fullmatch(r"[A-Z0-9]{2,24}", symbol)
                        or status not in STATUSES
                    ):
                        raise ValueError("invalid protection finding")
                    counts[status] += 1
                    # Raw client identifiers and scope labels never reach browser.
                    identity = hashlib.sha256(
                        (self._scope + ":" + row["client_id"]).encode()
                    ).hexdigest()[:24]
                    findings.append(
                        dict(
                            id=identity,
                            symbol=symbol,
                            status=status,
                            reasons=row["reasons"],
                        )
                    )
                return dict(
                    schema_version=1,
                    source="caller_exported_protection_inventory",
                    observed_at=datetime.now(timezone.utc).isoformat(),
                    captured_at=result["captured_at"],
                    controls="read_only",
                    safe_to_resume=False,
                    account_exposure_verified=False,
                    automatic_actions=[],
                    counts=counts,
                    findings=findings,
                    limitations=[
                        "export_provenance_not_authenticated",
                        "missing_orders_require_terminal_and_child_fill_evidence",
                        "no_alert_delivery_or_automatic_repair",
                    ],
                )
            except Exception:
                raise ProtectionObservationUnavailable(
                    "protection evidence unavailable"
                ) from None
