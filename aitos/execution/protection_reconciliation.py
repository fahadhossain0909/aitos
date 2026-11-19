"""Authenticated read-only protection inventory reconciliation.

This module joins the scoped local intent journal to Binance's authenticated
open-algo-order inventory. It is deliberately observational: a match is not
account-position proof, and this module never clears, retries, cancels or replaces
an order or journal intent.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from aitos.execution.intent_store import ExecutionUncertainError
from aitos.execution.position_integrity import assert_position_snapshot_matches
from aitos.execution.protection_audit import audit_protection_inventory


def _decimal_matches(left: Any, right: Any) -> bool:
    try:
        first, second = Decimal(str(left)), Decimal(str(right))
    except (InvalidOperation, ValueError):
        return False
    return first.is_finite() and second.is_finite() and first == second


async def query_ambiguous_protection_submissions(
    executor: Any,
    intent_rows: list[dict[str, Any]],
    *,
    open_client_ids: set[str],
    max_queries: int = 100,
) -> list[dict[str, Any]]:
    """Read-only venue lookup for SUBMITTING protective POST-to-ACK gaps."""
    candidates = []
    for row in intent_rows:
        if row.get("state") != "SUBMITTING":
            continue
        request = row.get("request_json")
        if isinstance(request, str):
            request = json.loads(request)
        if not isinstance(request, dict):
            raise ValueError("malformed protective preclaim")
        if request.get("order_type") not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
            continue
        client_id = row.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise ValueError("protective preclaim client identity is missing")
        if client_id not in open_client_ids:
            candidates.append((row, request, client_id))
    if len(candidates) > max_queries or not 1 <= max_queries <= 100:
        raise ValueError("ambiguous protection query budget exceeded")
    if not candidates:
        return []

    lookup = getattr(executor, "get_algo_order_by_client_id", None)
    child_lookup = getattr(executor, "get_order_status", None)
    if not callable(lookup) or not callable(child_lookup):
        raise TypeError("read-only algo client-id and child-order readers are required")

    findings = []
    for row, request, client_id in candidates:
        symbol = request["symbol"]
        algo = await lookup(symbol, client_id)
        if algo is None:
            findings.append(
                {
                    "client_id": client_id,
                    "symbol": symbol,
                    "status": "not_found_at_query_time",
                    "algo_id": None,
                    "child_status": None,
                    "executed_quantity": None,
                }
            )
            continue

        side = str(request.get("side", "")).upper()
        expected_order_side = "SELL" if side == "LONG" else "BUY"
        position_side = algo.get("positionSide")
        reduce_only = algo.get("reduceOnly")
        position_semantics_match = (
            position_side == "BOTH" and reduce_only is True
        ) or (position_side == side and reduce_only is False)
        identity_matches = (
            algo.get("clientAlgoId") == client_id
            and algo.get("symbol") == symbol
            and algo.get("side") == expected_order_side
            and algo.get("orderType") == request.get("order_type")
            and _decimal_matches(algo.get("quantity"), request.get("quantity"))
            and _decimal_matches(
                algo.get("triggerPrice"), request.get("reference_price")
            )
            and position_semantics_match
        )
        algo_id = str(algo.get("algoId"))
        if not identity_matches:
            findings.append(
                {
                    "client_id": client_id,
                    "symbol": symbol,
                    "status": "identity_mismatch",
                    "algo_id": algo_id,
                    "child_status": None,
                    "executed_quantity": None,
                }
            )
            continue

        algo_status = algo.get("algoStatus")
        child_status = None
        executed_quantity = None
        if algo.get("actualOrderId") not in (None, "", 0, "0"):
            try:
                child = await child_lookup(symbol, f"algo:{algo_id}")
            except ExecutionUncertainError:
                status = "child_execution_unconfirmed"
            else:
                child_status = child.get("status")
                executed_quantity = child.get("executedQty")
                status = "child_order_observed"
        elif algo_status == "NEW":
            status = "accepted_resting_order_found"
        elif algo_status in {"TRIGGERED", "FINISHED"}:
            status = "triggered_without_child_identity"
        else:
            status = "terminal_without_child_identity"
        findings.append(
            {
                "client_id": client_id,
                "symbol": symbol,
                "status": status,
                "algo_id": algo_id,
                "child_status": child_status,
                "executed_quantity": executed_quantity,
            }
        )
    return sorted(findings, key=lambda item: item["client_id"])


def _exchange_flag(value: Any, field: str) -> bool:
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise ValueError(f"invalid {field} in authenticated open-order evidence")


async def _audit_account_exposure(
    executor: Any, trades: Iterable[Any] | None
) -> dict[str, Any]:
    if trades is None:
        return {
            "position_snapshot_verified": False,
            "position_snapshot_error": "local trade snapshot was not supplied",
            "pending_entry_orders": [],
            "account_exposure_verified": False,
        }
    position_reader = getattr(executor, "get_position_risk", None)
    order_reader = getattr(executor, "get_open_orders", None)
    if not callable(position_reader) or not callable(order_reader):
        raise TypeError("full-account position and open-order readers are required")
    local_trades = tuple(trades)
    position_rows = await position_reader()
    position_error = None
    try:
        assert_position_snapshot_matches(
            local_trades, position_rows, hedge_mode=bool(executor.hedge_mode)
        )
    except (TypeError, ValueError) as exc:
        position_error = str(exc)

    orders = await order_reader()
    if not isinstance(orders, list) or len(orders) > 10000:
        raise ValueError("complete bounded ordinary open-order inventory required")
    local_sides: dict[str, set[str]] = {}
    for trade in local_trades:
        raw_side = getattr(trade, "side", "")
        side = str(getattr(raw_side, "value", raw_side)).upper()
        symbol = str(getattr(trade, "symbol", "")).strip().upper()
        if symbol and side in {"LONG", "SHORT"}:
            local_sides.setdefault(symbol, set()).add(side)

    pending_entries = []
    for order in orders:
        if not isinstance(order, dict):
            raise ValueError("malformed ordinary open-order evidence")
        symbol = order.get("symbol")
        side = order.get("side")
        position_side = order.get("positionSide")
        if (
            not isinstance(symbol, str)
            or not symbol
            or side not in {"BUY", "SELL"}
            or position_side not in {"BOTH", "LONG", "SHORT"}
        ):
            raise ValueError("invalid ordinary open-order identity")
        reduce_only = _exchange_flag(order.get("reduceOnly", False), "reduceOnly")
        close_position = _exchange_flag(
            order.get("closePosition", False), "closePosition"
        )
        hedge_close = (position_side == "LONG" and side == "SELL") or (
            position_side == "SHORT" and side == "BUY"
        )
        if reduce_only or close_position or hedge_close:
            continue
        # In one-way mode an opposite-side order can reduce or reverse a
        # position. Without reduceOnly=true it is deliberately treated as
        # unverified entry exposure, never silently netted against the position.
        direction = (
            "LONG"
            if position_side == "LONG" or (position_side == "BOTH" and side == "BUY")
            else "SHORT"
        )
        pending_entries.append(
            {
                "order_id": str(order.get("orderId", "")),
                "symbol": symbol,
                "side": direction,
                "status": str(order.get("status", "")),
                "reason": "entry order lacks a validated stop-risk projection",
            }
        )
    return {
        "position_snapshot_verified": position_error is None,
        "position_snapshot_error": position_error,
        "pending_entry_orders": sorted(
            pending_entries, key=lambda item: (item["symbol"], item["order_id"])
        ),
        "account_exposure_verified": position_error is None and not pending_entries,
    }


async def audit_exchange_protection_inventory(
    executor: Any,
    intent_store: Any,
    *,
    now: datetime | None = None,
    max_age_seconds: float = 120,
    trades: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Compare local protective intents with a fresh signed venue inventory.

    ``executor`` must expose ``execution_scope`` and the read-only
    ``get_open_algo_orders`` and ``get_algo_order_by_client_id`` methods.
    ``intent_store`` must expose the matching durable ``scope`` and
    ``protection_intents`` reader. The returned report never
    authorizes recovery; terminal child fills and account positions remain
    independent reconciliation requirements.
    """
    scope = getattr(intent_store, "scope", None)
    executor_scope = getattr(executor, "execution_scope", None)
    if not isinstance(scope, str) or not scope or executor_scope != scope:
        raise ValueError("executor and intent journal account scope mismatch")
    inventory_reader = getattr(executor, "get_open_algo_orders", None)
    journal_reader = getattr(intent_store, "protection_intents", None)
    if not callable(inventory_reader) or not callable(journal_reader):
        raise TypeError(
            "read-only inventory and protection-history readers are required"
        )

    captured_at = now or datetime.now(timezone.utc)
    if captured_at.utcoffset() is None:
        raise ValueError("timezone-aware inventory timestamp required")
    local_trades = tuple(trades) if trades is not None else None
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

    account_audit = await _audit_account_exposure(executor, local_trades)
    if not symbols:
        return {
            "schema_version": 1,
            "evidence": "AUTHENTICATED_READ_ONLY_EXCHANGE_API",
            "captured_at": captured_at.isoformat(),
            "age_seconds": 0.0,
            "scope_matched": True,
            **account_audit,
            "safe_to_resume": False,
            "automatic_actions": [],
            "findings": [],
            "ambiguous_submissions": [],
            "next_steps": [
                "No protective intents or open conditional orders were returned.",
                "Account positions and terminal order history remain separate "
                "evidence.",
            ],
        }

    evidence = {
        "scope": scope,
        "complete": True,
        "captured_at": captured_at.isoformat(),
        "symbols": sorted(symbols),
        "orders": orders,
        **(
            {
                "active_trade_ids": [
                    str(getattr(trade, "trade_id"))
                    for trade in local_trades
                    if getattr(trade, "trade_id", None)
                ]
            }
            if local_trades is not None
            else {}
        ),
    }
    report = audit_protection_inventory(
        rows,
        evidence,
        scope=scope,
        now=captured_at,
        max_age_seconds=max_age_seconds,
    )
    report.update(account_audit)
    report["evidence"] = "AUTHENTICATED_READ_ONLY_EXCHANGE_API"
    report["ambiguous_submissions"] = await query_ambiguous_protection_submissions(
        executor,
        rows,
        open_client_ids={
            str(order.get("clientAlgoId"))
            for order in orders
            if order.get("clientAlgoId")
        },
    )
    report["safe_to_resume"] = False
    return report



async def require_live_account_exposure_reconciled(
    executor: Any,
    intent_store: Any,
    trades: Iterable[Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fail closed unless full positions and entry-order inventory reconcile.

    The report remains observational. This guard never acknowledges, cancels,
    replaces or submits an order and never changes the intent journal.
    """
    report = await audit_exchange_protection_inventory(
        executor, intent_store, now=now, trades=trades
    )
    if not report.get("account_exposure_verified"):
        raise RuntimeError(
            "live account exposure is not reconciled: "
            f"position_error={report.get('position_snapshot_error')!r}, "
            f"pending_entry_orders={len(report.get('pending_entry_orders', []))}"
        )
    findings = report.get("findings", [])
    blocking = [
        finding
        for finding in findings
        if finding.get("status") != "applied_order_present"
    ]
    if blocking or report.get("ambiguous_submissions"):
        raise RuntimeError(
            "live protective-order inventory requires reconciliation: "
            f"blocking_findings={len(blocking)}, "
            f"ambiguous_submissions={len(report.get('ambiguous_submissions', []))}"
        )
    return report
