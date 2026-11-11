"""Pure paper-only amendment calculation/lineage contract; no DB or venue calls.

Fee = nonnegative cost; funding = signed cashflow. Matches existing lifecycle
net-PnL/percentage rounding. Receipt digests are references, not venue attestation.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from datetime import datetime

AMENDMENT_KEY = "_financial_amendment_v1"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _identifier(value):
    if type(value) is not str or not ID.fullmatch(value):
        raise ValueError("bounded amendment identity required")


def _number(value):
    try:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("finite amendment amount required")
    except OverflowError:
        raise ValueError("finite amendment amount required") from None
    return value


@dataclass(frozen=True)
class CostAmendment:
    source: str
    adjustment_id: str
    kind: str
    amount_usd: float
    recorded_at: str
    receipt_sha256: str

    def __post_init__(self):
        _identifier(self.source)
        _identifier(self.adjustment_id)
        if type(self.kind) is not str or self.kind not in {"fee", "funding"}:
            raise ValueError("only fee/funding amendments supported")
        amount = _number(self.amount_usd)
        if self.kind == "fee" and amount < 0:
            raise ValueError("fee cannot be negative; refunds need a separate contract")
        if type(self.recorded_at) is not str or "T" not in self.recorded_at:
            raise ValueError("aware amendment timestamp required")
        stamp = datetime.fromisoformat(self.recorded_at.replace("Z", "+00:00"))
        if stamp.utcoffset() is None:
            raise ValueError("aware amendment timestamp required")
        if type(self.receipt_sha256) is not str or not SHA256.fullmatch(
            self.receipt_sha256
        ):
            raise ValueError("receipt SHA-256 reference required")

    def metadata(self, parent_revision, parent_digest):
        if type(parent_revision) is not int or not 1 <= parent_revision < 2**63:
            raise ValueError("positive signed-64-bit parent revision required")
        if type(parent_digest) is not str or not SHA256.fullmatch(parent_digest):
            raise ValueError("parent digest required")
        return dict(
            schema_version=1,
            parent_revision=parent_revision,
            parent_digest=parent_digest,
            **self.__dict__,
        )


def decode_metadata(value):
    keys = {
        "schema_version",
        "parent_revision",
        "parent_digest",
        "source",
        "adjustment_id",
        "kind",
        "amount_usd",
        "recorded_at",
        "receipt_sha256",
    }
    if (
        type(value) is not dict
        or set(value) != keys
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("complete known amendment metadata required")
    command = CostAmendment(
        **{key: value[key] for key in CostAmendment.__dataclass_fields__}
    )
    command.metadata(value["parent_revision"], value["parent_digest"])
    return command


def apply_amendment(previous, metadata):
    """Return a new full snapshot. Never modify input or remove earlier evidence.

    Full Trade validation is performed by the typed adapter before commit and
    during recovery. This pure function validates the exact allowed delta.
    """
    command = decode_metadata(metadata)
    if type(previous) is not dict or previous.get("state") != "position_closed":
        raise ValueError("amendments require a closed Trade")
    result = copy.deepcopy(previous)
    result.pop(AMENDMENT_KEY, None)
    if type(result.get("cost_adjustments")) is not dict:
        raise ValueError("complete adjustment ledger required")
    if command.adjustment_id in result["cost_adjustments"]:
        raise ValueError(
            "adjustment ID already present; reconcile instead of charging again"
        )
    fees = _number(result["fees_paid_usd"])
    funding = _number(result["funding_pnl_usd"])
    gross = _number(result["gross_realized_pnl_usd"])
    if command.kind == "fee":
        fees += float(command.amount_usd)
    else:
        funding += float(command.amount_usd)
    net = round(gross - fees + funding, 8)
    notional = result["position_size_usd"] + sum(
        item["size_usd"] for item in result["partial_exits"]
    )
    percentage = round(net / notional * 100.0, 8) if notional > 0 else 0.0
    for amount in (fees, funding, gross, net, notional, percentage):
        _number(amount)
    if fees < 0 or notional < 0:
        raise ValueError("invalid recorded cost/notional")
    result.update(
        fees_paid_usd=fees,
        funding_pnl_usd=funding,
        pnl=net,
        pnl_percent=percentage,
        updated_at=command.recorded_at,
    )
    result["cost_adjustments"][command.adjustment_id] = dict(
        kind=command.kind,
        amount_usd=float(command.amount_usd),
        recorded_at=command.recorded_at,
    )
    result[AMENDMENT_KEY] = copy.deepcopy(metadata)
    return result
