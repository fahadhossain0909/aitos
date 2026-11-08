"""Typed, isolated paper recovery rehearsal. No runner, venue or migration wiring.

This module does not authorize orders. Monitoring/entry readiness is test evidence,
not enforcement in an existing application. Complete Trade snapshots only; legacy
rows are assessed separately and never silently upgraded or imported.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, fields
from datetime import datetime

from aitos.models.trade import PartialExit, Trade, TradeLifecycleState, TradeSide
from aitos.trading.revision_journal import (
    JournalIntegrityError,
    Revision,
    RevisionJournal,
    Scope,
    Writer,
    canonical,
    identifier,
)

from aitos.trading.financial_amendment import AMENDMENT_KEY, decode_metadata

FIELDS = {field.name for field in fields(Trade)}
STATES = {"position_opened", "exit_triggered", "position_closed"}
NUMERIC = {
    "entry_price",
    "quantity",
    "leverage",
    "position_size_usd",
    "risk_amount_usd",
    "sl_price",
    "tp_price",
    "gross_realized_pnl_usd",
    "fees_paid_usd",
    "funding_pnl_usd",
}
OPTIONAL_NUMERIC = {
    "breakeven_at_r_multiple",
    "exit_price",
    "pnl",
    "pnl_percent",
    "initial_r_distance",
    "mae_price",
    "mfe_price",
    "mae_r",
    "mfe_r",
    "emergency_sl_price",
}
TEXT = {
    "trade_id",
    "symbol",
    "strategy_id",
    "explanation",
    "regime",
    "entry_time",
    "updated_at",
}
OPTIONAL_TEXT = {
    "sl_order_id",
    "exit_time",
    "exit_reason",
    "rejection_reason",
    "last_marked_at",
    "adaptive_exit_last_bar_id",
}


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("finite numeric Trade field required")
    return value


def timestamp(value):
    if type(value) is not str or "T" not in value:
        raise ValueError("explicit timezone timestamp required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("explicit timezone timestamp required")
    return result


def decode_trade(data: dict) -> Trade:
    """Strict version-1 full snapshot; no coercion, hidden defaults or field loss."""
    canonical(data)
    if AMENDMENT_KEY in data:
        decode_metadata(data[AMENDMENT_KEY])
        data = {key: value for key, value in data.items() if key != AMENDMENT_KEY}
    if set(data) != FIELDS:
        raise ValueError("complete recognized Trade schema required")
    for key in TEXT:
        if type(data[key]) is not str or not data[key] or len(data[key]) > 4096:
            raise ValueError("bounded Trade text required")
    for key in OPTIONAL_TEXT:
        if data[key] is not None and (
            type(data[key]) is not str or len(data[key]) > 4096
        ):
            raise ValueError("invalid optional Trade text")
    identifier(data["trade_id"])
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_.:/-]{0,63}", data["symbol"]):
        raise ValueError("canonical symbol required")
    if type(data["state"]) is not str or data["state"] not in STATES:
        raise ValueError("persisted position state required")
    side = TradeSide(data["side"])
    for key in NUMERIC:
        number(data[key])
    for key in OPTIONAL_NUMERIC:
        if data[key] is not None:
            number(data[key])
    for key in ["entry_price", "leverage", "sl_price", "tp_price"]:
        if data[key] <= 0:
            raise ValueError("positive price/leverage required")
    for key in ["quantity", "position_size_usd", "risk_amount_usd", "fees_paid_usd"]:
        if data[key] < 0:
            raise ValueError("negative quantity/notional/risk/fee")
    if data["state"] != "position_closed" and (
        data["quantity"] <= 0 or data["position_size_usd"] <= 0
    ):
        raise ValueError("open position requires positive remaining exposure")
    if not math.isclose(
        data["position_size_usd"],
        data["quantity"] * data["entry_price"],
        rel_tol=1e-8,
        abs_tol=1e-8,
    ):
        raise ValueError("quantity/notional disagreement")
    for key in [
        "initial_r_distance",
        "mae_price",
        "mfe_price",
        "mae_r",
        "mfe_r",
        "emergency_sl_price",
        "breakeven_at_r_multiple",
    ]:
        if data[key] is not None and data[key] < 0:
            raise ValueError("negative risk/excursion field")
    for key in ["trailing_sl_enabled", "breakeven_triggered"]:
        if type(data[key]) is not bool:
            raise ValueError("boolean Trade flag required")
    if (
        type(data["adaptive_exit_observations"]) is not int
        or data["adaptive_exit_observations"] < 0
    ):
        raise ValueError("nonnegative integer observation count required")
    for key in ["entry_time", "updated_at", "exit_time", "last_marked_at"]:
        if data[key] is not None:
            timestamp(data[key])
    for key in ["agent_consensus", "cost_adjustments"]:
        if type(data[key]) is not dict:
            raise ValueError("Trade mapping required")
    if data["entry_thesis"] is not None and type(data["entry_thesis"]) is not dict:
        raise ValueError("invalid entry thesis")
    for key in ["take_profit_levels", "tp_order_ids", "partial_exits"]:
        if type(data[key]) is not list:
            raise ValueError("Trade list required")
    for value in data["take_profit_levels"]:
        if number(value) <= 0:
            raise ValueError("positive TP level required")
    for value in data["tp_order_ids"]:
        if type(value) is not str or not value or len(value) > 128:
            raise ValueError("bounded order ID required")
    partials = []
    for item in data["partial_exits"]:
        if type(item) is not dict or set(item) != {
            "price",
            "size_usd",
            "r_multiple",
            "at",
        }:
            raise ValueError("complete partial exit required")
        if number(item["price"]) <= 0 or number(item["size_usd"]) <= 0:
            raise ValueError("positive partial-exit evidence required")
        number(item["r_multiple"])
        timestamp(item["at"])
        partials.append(PartialExit(**item))
    for adjustment_id, item in data["cost_adjustments"].items():
        identifier(adjustment_id)
        if (
            type(item) is not dict
            or set(item) != {"kind", "amount_usd", "recorded_at"}
            or type(item["kind"]) is not str
            or item["kind"] not in {"fee", "funding"}
        ):
            raise ValueError("complete known cost adjustment required")
        amount = number(item["amount_usd"])
        timestamp(item["recorded_at"])
        if item["kind"] == "fee" and amount < 0:
            raise ValueError("negative fee adjustment")
    if data["pnl"] is not None:
        net = round(
            data["gross_realized_pnl_usd"]
            - data["fees_paid_usd"]
            + data["funding_pnl_usd"],
            8,
        )
        if not math.isfinite(net) or not math.isclose(
            data["pnl"], net, rel_tol=1e-10, abs_tol=1e-8
        ):
            raise ValueError("net PnL disagrees with recorded components")
    if data["state"] == "position_closed" and (
        data["exit_price"] is None
        or data["exit_price"] <= 0
        or data["exit_time"] is None
        or data["pnl"] is None
    ):
        raise ValueError("terminal fill/time/PnL evidence required")
    values = json.loads(canonical(data))
    values.update(
        side=side, state=TradeLifecycleState(values["state"]), partial_exits=partials
    )
    result = Trade(**values)
    if canonical(result.to_dict()) != canonical(data):
        raise ValueError("model normalization would alter recorded evidence")
    return result


def encode_trade(trade: Trade) -> dict:
    if type(trade) is not Trade:
        raise ValueError("Trade instance required")
    payload = trade.to_dict()
    decode_trade(payload)
    return json.loads(canonical(payload))


@dataclass(frozen=True)
class RecoverySnapshot:
    records: tuple[Revision, ...]

    @property
    def open_trades(self):
        return tuple(
            decode_trade(row.payload)
            for row in self.records
            if row.payload["state"] != "position_closed"
        )

    @property
    def closed_trades(self):
        return tuple(
            decode_trade(row.payload)
            for row in self.records
            if row.payload["state"] == "position_closed"
        )


@dataclass(frozen=True)
class RecoveryReadiness:
    snapshot: RecoverySnapshot
    registered: tuple[str, ...]
    failed: tuple[str, ...]
    feeds_missing: tuple[str, ...]
    recovery_prerequisites_met: bool
    writer_current: bool
    new_entries_allowed: bool = False
    live_authorized: bool = False


class PaperRevisionStore:
    """Explicit paper-only adapter; not registered with any running lifecycle."""

    def __init__(self, journal: RevisionJournal, writer: Writer):
        if (
            type(journal) is not RevisionJournal
            or type(writer) is not Writer
            or writer.scope.mode != "paper"
        ):
            raise ValueError("paper journal/writer required")
        self.journal = journal
        self.writer = writer

    def save_trade(self, trade: Trade, *, event_id: str, expected_revision: int):
        payload = encode_trade(trade)
        return self.journal.append(
            self.writer,
            trade_id=trade.trade_id,
            event_id=event_id,
            expected_revision=expected_revision,
            payload=payload,
        )

    def amend_cost(
        self, *, trade_id, event_id, expected_revision, expected_digest, amendment
    ):
        return self.journal.amend_closed(
            self.writer,
            trade_id=trade_id,
            event_id=event_id,
            expected_revision=expected_revision,
            expected_digest=expected_digest,
            amendment=amendment,
        )

    def recover(self, *, limit=1000):
        records = self.journal.heads(self.writer.scope, limit=limit)
        try:
            for row in records:
                decode_trade(row.payload)
        except (ValueError, TypeError, KeyError) as exc:
            raise JournalIntegrityError(
                "typed paper recovery requires valid complete Trade evidence"
            ) from exc
        return RecoverySnapshot(tuple(records))

    async def rehearse_monitoring(
        self, register, *, feed_status: dict, unresolved_intents: int, limit=1000
    ):
        """Test/local callback only. Does not enable entries in an actual runner.

        Attempt every monitor even if a peer registration fails. Keep all restored
        financial records; quarantine new entries until each trusted prerequisite
        is affirmed. Feed freshness/reconciliation must be independently supplied.
        """
        if (
            type(unresolved_intents) is not int
            or unresolved_intents < 0
            or type(feed_status) is not dict
        ):
            raise ValueError("explicit valid readiness inputs required")
        snapshot = self.recover(limit=limit)
        registered = []
        failed = []
        missing = []
        for trade in snapshot.open_trades:
            trade_id, symbol = trade.trade_id, trade.symbol
            original = canonical(encode_trade(trade))
            try:
                confirmed = await register(trade)
                if confirmed is not True or canonical(encode_trade(trade)) != original:
                    raise ValueError(
                        "monitor must confirm the unchanged restored record"
                    )
                registered.append(trade_id)
            except Exception:
                failed.append(trade_id)
            if feed_status.get(symbol) is not True:
                missing.append(symbol)
        # This report is not exchange reconciliation or order-fence certification.
        current = self.journal.is_current(self.writer)
        return RecoveryReadiness(
            snapshot,
            tuple(registered),
            tuple(failed),
            tuple(sorted(set(missing))),
            not failed and not missing and unresolved_intents == 0 and current,
            current,
        )


def assess_legacy_rows(rows, *, scope: Scope | None, limit=1000):
    """Read-only triage. Never import or claim lost merge history is reconstructed."""
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError("bounded assessment required")
    if scope is not None and type(scope) is not Scope:
        raise ValueError("trusted scope required")
    seen = {}
    issues = set()
    count = 0
    if scope is None:
        issues.add("scope_not_attested")
    for row in rows:
        count += 1
        if count > limit:
            raise ValueError("assessment limit exceeded; no partial report")
        try:
            if type(row) is not dict:
                raise ValueError()
            trade_id = identifier(row["trade_id"])
            stamp = timestamp(row["updated_at"])
            payload = row["payload"]
            if decode_trade(payload).trade_id != trade_id:
                raise ValueError()
            # Legacy millisecond key, not payload timestamp or insertion order.
            key = (
                trade_id,
                stamp.replace(microsecond=(stamp.microsecond // 1000) * 1000),
            )
            text = canonical(payload)
            if key in seen and seen[key] != text:
                issues.add("retained_timestamp_conflict")
            seen[key] = text
        except (ValueError, TypeError, KeyError):
            issues.add("invalid_or_incomplete_legacy_row")
    issues.add("history_not_attested")  # Replacement may already have erased history.
    return dict(
        schema_version=1,
        rows_examined=count,
        issues=sorted(issues),
        eligible_to_import=False,
        live_authorized=False,
        scope_present=scope is not None,
    )
