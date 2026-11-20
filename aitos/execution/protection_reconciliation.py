"""Authenticated read-only protection inventory reconciliation.

This module audits the scoped local intent journal against Binance's authenticated
open-algo-order inventory. Audit functions are observational. The separate,
explicit startup recovery function can only bind an exactly matched accepted
initial stop to its uniquely identified durable trade; it never retries, cancels
or replaces venue orders.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from aitos.execution.intent_store import ExecutionUncertainError
from aitos.execution.order_executor import OrderResult
from aitos.models.trade import TradeSide
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


def _audit_local_stop_orders(
    trades: tuple[Any, ...] | None,
    orders: list[dict[str, Any]],
    *,
    hedge_mode: bool,
) -> list[dict[str, Any]]:
    if trades is None:
        return []
    by_algo_id = {
        str(order.get("algoId")): order
        for order in orders
        if order.get("algoId") is not None
    }
    findings = []
    for trade in trades:
        trade_id = str(getattr(trade, "trade_id", "unknown"))
        symbol = str(getattr(trade, "symbol", "")).strip().upper()
        raw_side = getattr(trade, "side", "")
        side = str(getattr(raw_side, "value", raw_side)).upper()
        order_id = getattr(trade, "sl_order_id", None)
        if not isinstance(order_id, str) or not order_id:
            findings.append(
                {
                    "client_id": f"trade:{trade_id}",
                    "symbol": symbol,
                    "status": "local_position_missing_stop_id",
                    "reasons": ["active live position has no durable stop order ID"],
                }
            )
            continue
        if not order_id.startswith("algo:"):
            findings.append(
                {
                    "client_id": f"trade:{trade_id}",
                    "symbol": symbol,
                    "status": "unsupported_local_stop_identity",
                    "reasons": [
                        "live account audit requires a conditional algo stop ID"
                    ],
                }
            )
            continue
        algo_id = order_id.removeprefix("algo:")
        order = by_algo_id.get(algo_id)
        if order is None:
            findings.append(
                {
                    "client_id": f"trade:{trade_id}",
                    "symbol": symbol,
                    "status": "local_stop_missing_from_venue_inventory",
                    "reasons": [
                        "active position stop is absent from open algo inventory"
                    ],
                }
            )
            continue
        expected_side = "SELL" if side == "LONG" else "BUY"
        expected_position_side = side if hedge_mode else "BOTH"
        reasons = []
        if (
            not symbol
            or side not in {"LONG", "SHORT"}
            or order.get("symbol") != symbol
            or order.get("side") != expected_side
            or order.get("positionSide") != expected_position_side
            or order.get("orderType") != "STOP_MARKET"
        ):
            reasons.append("local stop symbol/side/type does not match active position")
        try:
            close_position = _exchange_flag(
                order.get("closePosition", False), "closePosition"
            )
            reduce_only = _exchange_flag(order.get("reduceOnly", False), "reduceOnly")
        except ValueError:
            close_position = False
            reduce_only = False
            reasons.append("local stop reduction flags are invalid")
        if hedge_mode and reduce_only:
            reasons.append(
                "hedge-mode stop must use positionSide rather than reduceOnly"
            )
        if not hedge_mode and not close_position and not reduce_only:
            reasons.append("one-way stop is not reduce-only")
        if not close_position and not _decimal_matches(
            order.get("quantity"), getattr(trade, "quantity", None)
        ):
            reasons.append("local stop quantity does not match active position")
        if reasons:
            findings.append(
                {
                    "client_id": f"trade:{trade_id}",
                    "symbol": symbol,
                    "status": "local_stop_identity_mismatch",
                    "reasons": reasons,
                }
            )
    return findings


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
    local_stop_findings = _audit_local_stop_orders(
        local_trades, orders, hedge_mode=bool(executor.hedge_mode)
    )
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
            "findings": local_stop_findings,
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
                    str(trade.trade_id)
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
    report["findings"] = sorted(
        [*report.get("findings", []), *local_stop_findings],
        key=lambda item: item["client_id"],
    )
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


async def recover_accepted_initial_stop_submissions(
    executor: Any,
    intent_store: Any,
    trades: Iterable[Any],
    persist_trade: Any,
) -> dict[str, Any]:
    """Recover a confirmed initial stop whose venue ACK/local write was interrupted.

    Recovery is deliberately narrow: only initial STOP_MARKET and
    TAKE_PROFIT_MARKET preclaims with an explicit owner_trade_id, a unique
    matching open trade, exact quantity/side, a fresh authenticated client-algo
    lookup, and an exact NEW reduce-only algo identity are eligible. Replacement
    stops, triggered orders, missing identities and mismatches remain
    quarantined for manual review.

    The trade snapshot is persisted before the journal transitions to APPLIED.
    A crash between those writes is idempotently recoverable on the next start.
    """
    scope = getattr(intent_store, "scope", None)
    if (
        not isinstance(scope, str)
        or not scope
        or getattr(executor, "execution_scope", None) != scope
    ):
        raise ValueError("executor and protective journal account scope mismatch")
    if not callable(persist_trade):
        raise TypeError("durable trade-state writer is required")
    lookup = getattr(executor, "get_algo_order_by_client_id", None)
    if not callable(lookup):
        raise TypeError("authenticated algo client-id lookup is required")
    rows = await asyncio.to_thread(intent_store.protection_intents)
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("bounded protective intent history is required")
    open_trades = tuple(trades)
    by_trade_id: dict[str, list[Any]] = {}
    for trade in open_trades:
        trade_id = str(getattr(trade, "trade_id", "") or "")
        if trade_id:
            by_trade_id.setdefault(trade_id, []).append(trade)

    handover_reader = getattr(intent_store, "protection_handovers", None)
    handover_rows = await asyncio.to_thread(handover_reader) if callable(handover_reader) else []
    handover_client_ids = {
        item.get("new_client_id")
        for item in handover_rows
        if isinstance(item, dict) and item.get("state") != "APPLIED"
    }
    recovered: list[str] = []
    blocked: list[dict[str, str]] = []
    for row in rows:
        if isinstance(row, dict) and row.get("client_id") in handover_client_ids:
            continue
        if not isinstance(row, dict) or row.get("state") not in {
            "SUBMITTING",
            "PROTECTED",
        }:
            continue
        request = row.get("request_json")
        if isinstance(request, str):
            request = json.loads(request)
        if not isinstance(request, dict):
            raise ValueError("malformed protective preclaim request")
        order_type = request.get("order_type")
        if order_type not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
            blocked.append(
                {
                    "client_id": str(row.get("client_id", "")),
                    "reason": "automatic recovery is limited to initial STOP_MARKET and TAKE_PROFIT_MARKET intents",
                }
            )
            continue
        client_id = row.get("client_id")
        owner_trade_id = request.get("owner_trade_id")
        if not isinstance(client_id, str) or not client_id:
            raise ValueError("protective client identity is missing")
        if not isinstance(owner_trade_id, str) or not owner_trade_id:
            blocked.append(
                {
                    "client_id": client_id,
                    "reason": "legacy protective preclaim has no owner_trade_id",
                }
            )
            continue
        owners = by_trade_id.get(owner_trade_id, [])
        if len(owners) != 1:
            blocked.append(
                {
                    "client_id": client_id,
                    "reason": "protective preclaim does not map to exactly one open trade",
                }
            )
            continue
        trade = owners[0]
        raw_side = getattr(trade, "side", "")
        trade_side = str(getattr(raw_side, "value", raw_side)).upper()
        request_side = str(request.get("side", "")).upper()
        symbol = str(getattr(trade, "symbol", "")).upper()
        if (
            not symbol
            or symbol != str(request.get("symbol", "")).upper()
            or trade_side not in {"LONG", "SHORT"}
            or request_side != trade_side
            or not _decimal_matches(
                getattr(trade, "quantity", None), request.get("quantity")
            )
            or request.get("reduce_only") is not True
        ):
            blocked.append(
                {
                    "client_id": client_id,
                    "reason": "owner trade does not match protective preclaim",
                }
            )
            continue

        algo = await lookup(symbol, client_id)
        if algo is None or algo.get("algoStatus") != "NEW":
            blocked.append(
                {
                    "client_id": client_id,
                    "reason": "venue has no confirmed NEW resting protective order",
                }
            )
            continue
        algo_id = algo.get("algoId")
        if (
            isinstance(algo_id, bool)
            or not str(algo_id or "").isdigit()
            or int(algo_id) <= 0
        ):
            blocked.append(
                {"client_id": client_id, "reason": "venue algo identity is invalid"}
            )
            continue
        order_id = f"algo:{int(algo_id)}"
        if row.get("state") == "PROTECTED":
            prior_result = row.get("result_json")
            if isinstance(prior_result, str):
                prior_result = json.loads(prior_result)
            if (
                not isinstance(prior_result, dict)
                or prior_result.get("order_id") != order_id
                or prior_result.get("client_order_id") != client_id
            ):
                blocked.append(
                    {
                        "client_id": client_id,
                        "reason": "durable accepted-stop result identity mismatch",
                    }
                )
                continue
        expected_side = "SELL" if trade_side == "LONG" else "BUY"
        expected_position_side = trade_side if bool(executor.hedge_mode) else "BOTH"
        try:
            quantity_matches = _decimal_matches(
                algo.get("quantity"), request.get("quantity")
            )
            trigger_matches = _decimal_matches(
                algo.get("triggerPrice"), request.get("reference_price")
            )
        except (TypeError, ValueError):
            quantity_matches = trigger_matches = False
        reduce_only = algo.get("reduceOnly")
        reduction_matches = algo.get("positionSide") == expected_position_side and (
            (bool(executor.hedge_mode) and reduce_only in (False, "false"))
            or (not bool(executor.hedge_mode) and reduce_only in (True, "true"))
        )
        if not (
            algo.get("clientAlgoId") == client_id
            and algo.get("symbol") == symbol
            and algo.get("algoType") == "CONDITIONAL"
            and algo.get("orderType") == order_type
            and algo.get("side") == expected_side
            and algo.get("closePosition") in (False, "false")
            and quantity_matches
            and trigger_matches
            and reduction_matches
        ):
            blocked.append(
                {
                    "client_id": client_id,
                    "reason": "authenticated venue protective-order identity mismatch",
                }
            )
            continue

        if order_type == "STOP_MARKET":
            existing_stop_id = getattr(trade, "sl_order_id", None)
            if existing_stop_id not in (None, "", order_id):
                blocked.append(
                    {
                        "client_id": client_id,
                        "reason": "trade already owns a different stop; replacement recovery is manual",
                    }
                )
                continue
            trade.sl_order_id = order_id
        else:
            existing_tp_ids = getattr(trade, "tp_order_ids", None)
            if not isinstance(existing_tp_ids, list):
                blocked.append(
                    {
                        "client_id": client_id,
                        "reason": "trade has no durable take-profit order collection",
                    }
                )
                continue
            if order_id not in existing_tp_ids:
                existing_tp_ids.append(order_id)

        # Write the venue identity to the durable trade snapshot first. If
        # the process dies before journal acknowledgement, the next pass sees
        # the same ID and safely completes the journal transition.
        trade.updated_at = datetime.now(timezone.utc).isoformat()
        await persist_trade(trade)
        if row.get("state") == "SUBMITTING":
            result = OrderResult(
                order_id=order_id,
                symbol=symbol,
                side=TradeSide(trade_side),
                filled_quantity=0.0,
                fill_price=float(request["reference_price"]),
                client_order_id=client_id,
            )
            await asyncio.to_thread(intent_store.record_protection, client_id, result)
        await asyncio.to_thread(intent_store.mark_applied, client_id, owner_trade_id)
        recovered.append(client_id)

    return {
        "recovered": len(recovered),
        "recovered_client_ids": recovered,
        "blocked": blocked,
        "safe_to_resume": False,
    }


async def recover_protection_handovers(
    executor: Any,
    intent_store: Any,
    trades: Iterable[Any],
    persist_trade: Any,
) -> dict[str, Any]:
    """Resume replacement-stop handovers without ever dropping the old stop early.

    PREPARED recovery uses the deterministic clientAlgoId to discover a POST
    whose response was lost. The new stop must still be NEW and match the exact
    durable request before the old stop can be cancelled. Old-stop cancellation
    is queried before and after DELETE so a triggered/partial fill is quarantined.
    """
    scope = getattr(intent_store, "scope", None)
    if (
        not isinstance(scope, str)
        or not scope
        or getattr(executor, "execution_scope", None) != scope
    ):
        raise ValueError("executor and handover journal account scope mismatch")
    if not callable(persist_trade):
        raise TypeError("durable trade-state writer is required")
    lookup = getattr(executor, "get_algo_order_by_client_id", None)
    cancel = getattr(executor, "cancel_resting_order", None)
    status_reader = getattr(executor, "get_resting_order_status", None)
    if not all(callable(item) for item in (lookup, cancel, status_reader)):
        raise TypeError("authenticated algo lookup and resting-order reconciliation are required")
    handovers = await asyncio.to_thread(intent_store.protection_handovers)
    intents = await asyncio.to_thread(intent_store.protection_intents)
    if (
        not isinstance(handovers, list)
        or len(handovers) > 10000
        or not isinstance(intents, list)
        or len(intents) > 10000
    ):
        raise ValueError("bounded protective recovery history is required")
    intent_by_client = {
        row.get("client_id"): row for row in intents if isinstance(row, dict)
    }
    by_trade_id: dict[str, list[Any]] = {}
    for trade in tuple(trades):
        trade_id = str(getattr(trade, "trade_id", "") or "")
        if trade_id:
            by_trade_id.setdefault(trade_id, []).append(trade)

    recovered: list[str] = []
    blocked: list[dict[str, str]] = []
    for handover in handovers:
        if not isinstance(handover, dict):
            raise ValueError("malformed protective handover row")
        state = handover.get("state")
        handover_id = handover.get("handover_id")
        if state == "APPLIED":
            continue
        client_id = str(handover.get("new_client_id", ""))
        if state == "BLOCKED":
            blocked.append(
                {
                    "client_id": client_id,
                    "reason": str(
                        handover.get("blocked_reason") or "handover is quarantined"
                    ),
                }
            )
            continue
        trade_id = str(handover.get("trade_id", ""))
        owners = by_trade_id.get(trade_id, [])
        if len(owners) != 1:
            blocked.append(
                {
                    "client_id": client_id,
                    "reason": "handover does not map to exactly one restored open trade",
                }
            )
            continue
        trade = owners[0]
        symbol = str(getattr(trade, "symbol", "")).upper()
        old_id = handover.get("old_order_id")
        intent = intent_by_client.get(client_id)
        if (
            not isinstance(handover_id, str)
            or not isinstance(old_id, str)
            or not old_id
            or symbol != str(handover.get("symbol", "")).upper()
            or not isinstance(intent, dict)
        ):
            blocked.append(
                {
                    "client_id": client_id,
                    "reason": "handover or linked protective intent identity is incomplete",
                }
            )
            continue
        request = intent.get("request_json")
        if isinstance(request, str):
            request = json.loads(request)
        if (
            not isinstance(request, dict)
            or request.get("owner_trade_id") != trade_id
            or request.get("symbol") != symbol
            or request.get("order_type") != "STOP_MARKET"
            or request.get("reduce_only") is not True
            or str(request.get("side", "")).upper()
            != str(
                getattr(getattr(trade, "side", ""), "value", getattr(trade, "side", ""))
            ).upper()
            or not _decimal_matches(
                request.get("quantity"), getattr(trade, "quantity", None)
            )
        ):
            blocked.append(
                {
                    "client_id": client_id,
                    "reason": "linked replacement intent does not match restored trade",
                }
            )
            continue

        algo = await lookup(symbol, client_id)
        if not isinstance(algo, dict) or algo.get("algoStatus") != "NEW":
            blocked.append(
                {
                    "client_id": client_id,
                    "reason": "replacement algo is not confirmed NEW; old stop remains untouched",
                }
            )
            continue
        algo_id = algo.get("algoId")
        if (
            isinstance(algo_id, bool)
            or not str(algo_id or "").isdigit()
            or int(algo_id) <= 0
        ):
            blocked.append(
                {"client_id": client_id, "reason": "replacement algo ID is invalid"}
            )
            continue
        new_id = f"algo:{int(algo_id)}"
        if handover.get("new_order_id") not in (None, "", new_id):
            blocked.append({
                    "client_id": client_id,
                    "reason": "replacement algo ID conflicts with durable handover",
                })
            continue
        trade_side = str(
            getattr(getattr(trade, "side", ""), "value", getattr(trade, "side", ""))
        ).upper()
        expected_side = "SELL" if trade_side == "LONG" else "BUY"
        expected_position_side = trade_side if bool(executor.hedge_mode) else "BOTH"
        try:
            exact_quantity = _decimal_matches(
                algo.get("quantity"), request.get("quantity")
            )
            exact_trigger = _decimal_matches(
                algo.get("triggerPrice"), request.get("reference_price")
            )
        except (TypeError, ValueError):
            exact_quantity = exact_trigger = False
        reduce_only = algo.get("reduceOnly")
        exact_reduction = algo.get("positionSide") == expected_position_side and (
            (bool(executor.hedge_mode) and reduce_only in (False, "false"))
            or (not bool(executor.hedge_mode) and reduce_only in (True, "true"))
        )
        if not (
            algo.get("clientAlgoId") == client_id
            and algo.get("symbol") == symbol
            and algo.get("algoType") == "CONDITIONAL"
            and algo.get("orderType") == "STOP_MARKET"
            and algo.get("side") == expected_side
            and algo.get("closePosition") in (False, "false")
            and exact_quantity
            and exact_trigger
            and exact_reduction
        ):
            blocked.append({
                    "client_id": client_id,
                    "reason": "replacement venue identity mismatch",
                })
            continue

        if state == "PREPARED":
            await asyncio.to_thread(
                intent_store.record_handover_new_order, handover_id, new_id
            )
            state = "NEW_ACCEPTED"
        elif handover.get("new_order_id") != new_id:
            blocked.append({
                    "client_id": client_id,
                    "reason": "durable replacement order ID is missing",
                })
            continue

        intent_state = intent.get("state")
        if intent_state == "PROTECTED":
            result_json = intent.get("result_json")
            if isinstance(result_json, str):
                result_json = json.loads(result_json)
            if not isinstance(result_json, dict) or result_json.get("order_id") != new_id:
                blocked.append(
                    {
                        "client_id": client_id,
                        "reason": "replacement intent result identity mismatch",
                    }
                )
                continue
        elif intent_state not in {"SUBMITTING", "APPLIED"}:
            blocked.append(
                {
                    "client_id": client_id,
                    "reason": "replacement intent is in an unsupported state",
                }
            )
            continue

        current_stop = getattr(trade, "sl_order_id", None)
        if current_stop not in (old_id, new_id):
            blocked.append({
                    "client_id": client_id,
                    "reason": "restored trade stop is neither old nor replacement stop",
                })
            continue
        trade.sl_order_id = new_id
        pending = trade.agent_consensus.setdefault("_pending_stop_cancellations", [])
        if not isinstance(pending, list):
            blocked.append(
                {
                    "client_id": client_id,
                    "reason": "durable pending-stop cancellation collection is malformed",
                }
            )
            continue
        if old_id not in pending:
            pending.append(old_id)
        trade.updated_at = datetime.now(timezone.utc).isoformat()
        await persist_trade(trade)

        if intent_state == "SUBMITTING":
            result = OrderResult(
                order_id=new_id,
                symbol=symbol,
                side=TradeSide(trade_side),
                filled_quantity=0.0,
                fill_price=float(request["reference_price"]),
                client_order_id=client_id,
            )
            await asyncio.to_thread(intent_store.record_protection, client_id, result)

        if state == "NEW_ACCEPTED":
            await asyncio.to_thread(
                intent_store.mark_handover_old_cancel_requested, handover_id
            )
            state = "OLD_CANCEL_REQUESTED"
        if state == "OLD_CANCEL_REQUESTED":
            old_status = await status_reader(symbol, old_id)
            if old_status in {"FILLED", "PARTIALLY_FILLED_TERMINAL"}:
                blocked.append(
                    {
                        "client_id": client_id,
                        "reason": "old stop has triggered or partially filled; child-fill reconciliation required",
                    }
                )
                continue
            if old_status not in {
                "CANCELED",
                "EXPIRED",
                "EXPIRED_IN_MATCH",
                "REJECTED",
            }:
                if old_status != "NEW":
                    blocked.append(
                        {
                            "client_id": client_id,
                            "reason": f"old stop status is ambiguous: {old_status!r}",
                        }
                    )
                    continue
                await cancel(symbol, old_id)
                old_status = await status_reader(symbol, old_id)
                if old_status in {"FILLED", "PARTIALLY_FILLED_TERMINAL"}:
                    blocked.append(
                        {
                            "client_id": client_id,
                            "reason": "old stop triggered during cancellation; child-fill reconciliation required",
                        }
                    )
                    continue
                if old_status not in {
                    "CANCELED",
                    "EXPIRED",
                    "EXPIRED_IN_MATCH",
                    "REJECTED",
                }:
                    blocked.append(
                        {
                            "client_id": client_id,
                            "reason": f"old stop cancellation is unconfirmed: {old_status!r}",
                        }
                    )
                    continue
            await asyncio.to_thread(
                intent_store.mark_handover_old_cancelled, handover_id
            )
            state = "OLD_CANCELLED"

        if state == "OLD_CANCELLED":
            while old_id in pending:
                pending.remove(old_id)
            trade.updated_at = datetime.now(timezone.utc).isoformat()
            await persist_trade(trade)
            await asyncio.to_thread(
                intent_store.complete_protection_handover, handover_id
            )
            if intent_state in {"SUBMITTING", "PROTECTED"}:
                executor.acknowledge_protection(
                    OrderResult(
                        order_id=new_id,
                        symbol=symbol,
                        side=TradeSide(trade_side),
                        filled_quantity=0.0,
                        fill_price=float(request["reference_price"]),
                        client_order_id=client_id,
                    ),
                    trade_id,
                )
            recovered.append(handover_id)

    return {
        "recovered": len(recovered),
        "recovered_handover_ids": recovered,
        "blocked": blocked,
        "safe_to_resume": False,
    }


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
