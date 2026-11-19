"""Authenticated read-only protection inventory reconciliation.

This module joins the scoped local intent journal to Binance's authenticated
open-algo-order inventory. It is deliberately observational: a match is not
account-position proof, and this module never clears, retries, cancels or replaces
an order or journal intent.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from aitos.execution.protection_audit import audit_protection_inventory


async def audit_exchange_protection_inventory(
    executor: Any,
    intent_store: Any,
    *,
    now: datetime | None = None,
    max_age_seconds: float = 120,
) -> dict[str, Any]:
    """Compare local protective intents with a fresh signed venue inventory.

    ``executor`` must expose ``execution_scope`` and the read-only
    ``get_open_algo_orders`` method. ``intent_store`` must expose the matching
    durable ``scope`` and ``unresolved`` reader. The returned report never
    authorizes recovery; terminal child fills and account positions remain
    independent reconciliation requirements.
    """
    scope = getattr(intent_store, "scope", None)
    executor_scope = getattr(executor, "execution_scope", None)
    if not isinstance(scope, str) or not scope or executor_scope != scope:
        raise ValueError("executor and intent journal account scope mismatch")
    inventory_reader = getattr(executor, "get_open_algo_orders", None)
    journal_reader = getattr(intent_store, "unresolved", None)
    if not callable(inventory_reader) or not callable(journal_reader):
        raise TypeError("read-only inventory and journal readers are required")

    captured_at = now or datetime.now(timezone.utc)
    if captured_at.utcoffset() is None:
        raise ValueError("timezone-aware inventory timestamp required")
    rows = await asyncio.to_thread(journal_reader)
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("bounded durable intent inventory required")

    # Validate and retain only symbols owned by protective intents. The audit
    # function itself enforces the complete protective request contract.
    symbols: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("malformed durable intent row")
        request = row.get("request_json")
        if isinstance(request, str):
            request = json.loads(request)
        if not isinstance(request, dict):
            raise ValueError("malformed durable intent request")
        if request.get("order_type") in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
            symbol = request.get("symbol")
            if not isinstance(symbol, str) or not symbol:
                raise ValueError("protective intent symbol is missing")
            symbols.add(symbol)

    orders = await inventory_reader()
    if not isinstance(orders, list) or len(orders) > 10000:
        raise ValueError("complete authenticated algo inventory required")
    for order in orders:
        if not isinstance(order, dict):
            raise ValueError("malformed authenticated algo inventory row")
        symbol = order.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("authenticated algo inventory symbol is missing")
        symbols.add(symbol)

    if not symbols:
        return {
            "schema_version": 1,
            "evidence": "AUTHENTICATED_READ_ONLY_EXCHANGE_API",
            "captured_at": captured_at.isoformat(),
            "age_seconds": 0.0,
            "scope_matched": True,
            "account_exposure_verified": False,
            "safe_to_resume": False,
            "automatic_actions": [],
            "findings": [],
            "next_steps": [
                "No protective intents or open conditional orders were returned.",
                "Account positions and terminal order history remain separate evidence.",
            ],
        }

    evidence = {
        "scope": scope,
        "complete": True,
        "captured_at": captured_at.isoformat(),
        "symbols": sorted(symbols),
        "orders": orders,
    }
    return audit_protection_inventory(
        rows,
        evidence,
        scope=scope,
        now=captured_at,
        max_age_seconds=max_age_seconds,
        evidence_source="AUTHENTICATED_READ_ONLY_EXCHANGE_API",
    )
