"""Exact, read-only comparison of externally supplied complete account snapshots.

No provider IO, credential access, mutations or execution authority. Matching
snapshots do not authenticate provider receipts or prove pagination completeness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

MONEY = re.compile(r"-?(?:0|[1-9][0-9]{0,23})(?:\.[0-9]{1,18})?\Z")
NAME = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")


def name(value):
    if type(value) is not str or not NAME.fullmatch(value):
        raise ValueError("invalid snapshot identity")
    return value


def amount(value):
    if type(value) is not str or not MONEY.fullmatch(value):
        raise ValueError("exact bounded decimal string required")
    return Decimal(value)


def stamp(value):
    if type(value) is not str or len(value) > 64 or "T" not in value:
        raise ValueError("aware timestamp required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("aware timestamp required")
    return result


@dataclass(frozen=True, repr=False)
class AccountSnapshot:
    identity: tuple = field(repr=False)
    as_of: datetime
    observed_at: datetime
    balances: tuple = field(repr=False)
    positions: tuple = field(repr=False)
    receipts: tuple = field(repr=False)

    @classmethod
    def parse(cls, value, *, now=None):
        now = now or datetime.now(timezone.utc)
        if type(value) is not dict:
            raise ValueError("recognized snapshot required")
        version = value.get("schema_version")
        extra = (
            {"position_mode", "receipt_window"}
            if type(version) is int and version == 2
            else set()
        )
        if (
            set(value)
            != {
                "schema_version",
                "provider",
                "account",
                "mode",
                "as_of",
                "observed_at",
                "complete",
                "balances",
                "positions",
                "receipts",
            }
            | extra
        ):
            raise ValueError("complete recognized snapshot schema required")
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] not in (1, 2)
            or value["complete"] is not True
            or value["mode"] not in ("paper", "testnet")
        ):
            raise ValueError("complete paper/testnet snapshot required")
        identity = (name(value["provider"]), name(value["account"]), value["mode"])
        observed, cut = stamp(value["observed_at"]), stamp(value["as_of"])
        if (
            not -5 <= (now - observed).total_seconds() <= 60
            or not 0 <= (observed - cut).total_seconds() <= 60
        ):
            raise ValueError("stale or inconsistent snapshot cut")
        if version == 2:
            position_mode = value["position_mode"]
            window = value["receipt_window"]
            if (
                position_mode not in ("ONE_WAY", "HEDGE")
                or type(window) is not dict
                or set(window) != {"start_ms", "end_ms"}
            ):
                raise ValueError("explicit position mode and receipt window required")
            start, end = window["start_ms"], window["end_ms"]
            delta = cut - datetime(1970, 1, 1, tzinfo=timezone.utc)
            cut_ms = (
                delta.days * 86400000
                + delta.seconds * 1000
                + delta.microseconds // 1000
            )
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= start <= end <= cut_ms
                or end - start >= 604800000
            ):
                raise ValueError("bounded receipt window required")
            identity = (*identity, 2, position_mode, start, end)
        else:
            identity = (*identity, 1)
        result = []
        one_way_symbols = set()
        for group, fields, keys in [
            ("balances", {"asset", "total", "available"}, ("asset",)),
            ("positions", {"symbol", "side", "quantity"}, ("symbol", "side")),
            ("receipts", {"receipt_id", "currency", "amount"}, ("receipt_id",)),
        ]:
            rows = value[group]
            if type(rows) is not list or len(rows) > 10000:
                raise ValueError("bounded complete rows required")
            seen = set()
            out = []
            for row in rows:
                if type(row) is not dict or set(row) != fields:
                    raise ValueError("recognized row required")
                key = tuple(name(row[k]) for k in keys)
                if key in seen:
                    raise ValueError("duplicate identity")
                seen.add(key)
                if group == "balances":
                    total, available = amount(row["total"]), amount(row["available"])
                    if version == 1 and not 0 <= available <= total:
                        raise ValueError("invalid balance")
                    values = (total, available)
                elif group == "positions":
                    if version == 2 and value["position_mode"] == "ONE_WAY":
                        if row["symbol"] in one_way_symbols:
                            raise ValueError("opposite legs in one-way evidence")
                        one_way_symbols.add(row["symbol"])
                    quantity = amount(row["quantity"])
                    if row["side"] not in ("LONG", "SHORT") or quantity <= 0:
                        raise ValueError("invalid position")
                    values = (quantity,)
                else:
                    values = (name(row["currency"]), amount(row["amount"]))
                out.append((*key, *values))
            result.append(tuple(sorted(out)))
        return cls(identity, cut, observed, *result)


def reconcile_accounts(expected, observed, *, now=None):
    """Return sanitized differences; malformed/stale input is never a match."""
    base = dict(read_only=True, live_authorized=False, production_certified=False)
    try:
        left, right = (AccountSnapshot.parse(v, now=now) for v in (expected, observed))
        if left.identity != right.identity or left.as_of != right.as_of:
            raise ValueError("different scope or cut")
    except (ValueError, TypeError, KeyError, OverflowError):
        return dict(base, status="unavailable", matched=False, differences=None)
    differences = {
        key: len(set(getattr(left, key)) ^ set(getattr(right, key)))
        for key in ("balances", "positions", "receipts")
    }
    matched = not any(differences.values())
    return dict(
        base,
        status="matched" if matched else "mismatch",
        matched=matched,
        differences=differences,
    )


def read_snapshot_file(path):
    """Bounded strict JSON loader shared by offline reconciliation commands."""
    import json

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def nonfinite(value):
        raise ValueError("nonfinite JSON constant")

    with path.open("rb") as stream:
        body = stream.read(4 * 1024 * 1024 + 1)
    if len(body) > 4 * 1024 * 1024:
        raise ValueError("snapshot size exceeded")
    return json.loads(body, object_pairs_hook=unique, parse_constant=nonfinite)


def main(argv=None):
    """Offline files only; print sanitized result, never account/receipt contents."""
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("expected", type=Path)
    parser.add_argument("observed", type=Path)
    args = parser.parse_args(argv)

    try:
        report = reconcile_accounts(
            read_snapshot_file(args.expected), read_snapshot_file(args.observed)
        )
    except (OSError, ValueError, TypeError, RecursionError):
        report = dict(
            read_only=True,
            live_authorized=False,
            production_certified=False,
            status="unavailable",
            matched=False,
            differences=None,
        )
    print(json.dumps(report, sort_keys=True))
    return (
        0
        if report["status"] == "matched"
        else 2 if report["status"] == "mismatch" else 3
    )


if __name__ == "__main__":
    raise SystemExit(main())
