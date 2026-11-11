"""Offline conditional-order evidence comparison. Never clears a quarantine.

An exported open-order list cannot establish absence of fills or account exposure.
A matching resting order is a reconciliation candidate, not a recovery approval.
No exchange connection, order mutation or journal write exists in this module.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

PROTECTION_TYPES = {"STOP_MARKET", "TAKE_PROFIT_MARKET"}


def _positive(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number > 0 else None
    except (InvalidOperation, ValueError):
        return None


def _object(value: Any) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("expected evidence object")
    return value


def audit_protection_inventory(
    intents: list[dict],
    evidence: dict,
    *,
    scope: str,
    now: datetime | None = None,
    max_age_seconds: float = 120,
) -> dict:
    """Compare scoped journal intents with a caller-supplied exported snapshot.

    The envelope must explicitly declare completeness and covered symbols.
    Caller declarations are not independently authenticated venue evidence.
    Reject stale/cross-account/incomplete input before interpreting missing IDs.
    """
    if not scope or evidence.get("scope") != scope:
        raise ValueError("protection evidence account scope mismatch")
    if evidence.get("complete") is not True:
        raise ValueError("complete inventory declaration required")
    if not math.isfinite(max_age_seconds) or not 0 < max_age_seconds <= 300:
        raise ValueError("invalid freshness bound")
    now = now or datetime.now(timezone.utc)
    try:
        captured = datetime.fromisoformat(
            evidence["captured_at"].replace("Z", "+00:00")
        )
        if captured.utcoffset() is None or now.utcoffset() is None:
            raise ValueError()
        age = (now - captured).total_seconds()
        if not 0 <= age <= max_age_seconds:
            raise ValueError()
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError(
            "inventory timestamp is missing, stale or in the future"
        ) from None
    orders = evidence.get("orders")
    symbols = evidence.get("symbols")
    if not isinstance(orders, list) or len(orders) > 10000:
        raise ValueError("bounded order inventory required")
    if (
        not isinstance(symbols, list)
        or not symbols
        or not all(isinstance(s, str) and s for s in symbols)
    ):
        raise ValueError("covered symbols required")
    if len(intents) > 10000:
        raise ValueError("bounded journal inventory required")

    expected = {}
    findings = []
    for row in intents:
        if row.get("scope", scope) != scope:
            raise ValueError("journal account scope mismatch")
        request = _object(row.get("request_json", {}))
        if (
            request.get("order_type") not in PROTECTION_TYPES
            or row.get("state") == "REJECTED"
        ):
            continue
        cid = row.get("client_id")
        if not isinstance(cid, str) or not cid or cid in expected:
            raise ValueError("unique journal client identity required")
        if request.get("symbol") not in symbols:
            raise ValueError("inventory does not cover journal symbols")
        if (
            request.get("side") not in {"LONG", "SHORT", "long", "short"}
            or request.get("reduce_only") is not True
        ):
            raise ValueError("invalid protective journal contract")
        if (
            _positive(request.get("quantity")) is None
            or _positive(request.get("reference_price")) is None
        ):
            raise ValueError("invalid protective journal quantities")
        if row.get("state") not in {"SUBMITTING", "PROTECTED", "APPLIED"}:
            raise ValueError("unsupported protective journal state")
        if request.get("client_order_id", cid) != cid:
            raise ValueError("journal client identity mismatch")
        expected[cid] = (row, request)

    seen = set()
    seen_exchange_ids = set()
    for order in orders:
        if not isinstance(order, dict):
            raise ValueError("invalid inventory order")
        cid = order.get("clientAlgoId")
        oid = order.get("algoId")
        if (
            not isinstance(cid, str)
            or not cid
            or isinstance(oid, bool)
            or not isinstance(oid, (int, str))
            or not re.fullmatch(r"[0-9]{1,30}", str(oid))
            or int(oid) <= 0
        ):
            raise ValueError("algo identity required")
        if cid in seen or str(oid) in seen_exchange_ids:
            raise ValueError("duplicate inventory identity")
        if order.get("symbol") not in symbols:
            raise ValueError("order outside declared symbol coverage")
        seen.add(cid)
        seen_exchange_ids.add(str(oid))
        if cid not in expected:
            findings.append(
                {
                    "client_id": cid,
                    "symbol": order.get("symbol"),
                    "status": "unowned_order",
                    "reasons": ["no protective journal owner"],
                }
            )
            continue
        row, request = expected[cid]
        reasons = []
        side = request["side"].upper()
        expected_side = "SELL" if side == "LONG" else "BUY"
        for field, value in (
            ("symbol", request["symbol"]),
            ("side", expected_side),
            ("orderType", request["order_type"]),
            ("algoStatus", "NEW"),
        ):
            if order.get(field) != value:
                reasons.append(f"{field} mismatch")
        for field, value in (
            ("quantity", request["quantity"]),
            ("triggerPrice", request["reference_price"]),
        ):
            actual = _positive(order.get(field))
            if actual is None or actual != _positive(value):
                reasons.append(f"{field} mismatch")
        position_side = order.get("positionSide")
        if position_side == "BOTH":
            if not (
                order.get("reduceOnly") is True or order.get("reduceOnly") == "true"
            ):
                reasons.append("reduceOnly evidence missing")
        elif position_side != side:
            reasons.append("positionSide evidence mismatch")
        result = _object(row.get("result_json") or {})
        if result.get("order_id") and result["order_id"] != f"algo:{oid}":
            reasons.append("durable algo identity mismatch")
        status = (
            "mismatch"
            if reasons
            else (
                "applied_order_present"
                if row.get("state") == "APPLIED"
                else "unapplied_match"
            )
        )
        findings.append(
            {
                "client_id": cid,
                "symbol": request["symbol"],
                "status": status,
                "reasons": reasons,
            }
        )
    for cid, (row, request) in expected.items():
        if cid not in seen:
            findings.append(
                {
                    "client_id": cid,
                    "symbol": request["symbol"],
                    "status": "not_in_open_inventory",
                    "reasons": [
                        "may be filled, canceled, expired or absent; query terminal/child evidence"
                    ],
                }
            )
    return {
        "schema_version": 1,
        "evidence": "CALLER_EXPORTED_INVENTORY",
        "captured_at": captured.isoformat(),
        "age_seconds": age,
        "scope_matched": True,
        "account_exposure_verified": False,
        "safe_to_resume": False,
        "automatic_actions": [],
        "findings": sorted(findings, key=lambda item: item["client_id"]),
        "next_steps": [
            "Verify export provenance and current account positions independently.",
            "Query terminal algo and actual child fill evidence for missing orders.",
            "Durably bind accepted protective IDs to trade state before any acknowledgement.",
            "Do not clear intents or submit replacement orders based on this report alone.",
        ],
    }
