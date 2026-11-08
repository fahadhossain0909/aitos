"""Offline Binance-shaped account evidence adapter. No client, keys or network.

Structural enumeration checks do not authenticate a provider or prove absence of
missing pages/rows. Schema 2 binds position mode and inclusive receipt window.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from aitos.trading.account_reconciliation import (
    AccountSnapshot,
    amount,
    name,
    reconcile_accounts,
)

ASSET = re.compile(r"[A-Z0-9]{1,20}\Z")
MAX_MS = 253402300799999
TYPES = {"COMMISSION", "FUNDING_FEE", "REALIZED_PNL", "TRANSFER"}


def integer(value):
    if type(value) is not int or not 0 <= value <= MAX_MS:
        raise ValueError("bounded millisecond integer required")
    return value


def asset(value):
    if type(value) is not str or not ASSET.fullmatch(value):
        raise ValueError("recognized currency/symbol required")
    return value


def receipt_id(value):
    if type(value) is int and 0 <= value <= 2**63 - 1:
        return str(value)
    if (
        type(value) is str
        and re.fullmatch(r"(?:0|[1-9][0-9]{0,18})", value)
        and int(value) <= 2**63 - 1
    ):
        return value
    raise ValueError("exact receipt identity required")


def adapt_recorded_account(bundle, *, now=None):
    """Return a new validated schema-2 snapshot, never mutate provider responses.

    Caller must supply complete captured balances, position risk and paginated
    income responses for a common cut. Only paper/testnet evidence is accepted.
    Raw extra provider fields are discarded, not copied to normalized snapshots.
    """
    if type(bundle) is not dict or set(bundle) != {
        "schema_version",
        "account",
        "mode",
        "server_time_ms",
        "observed_at",
        "position_mode",
        "income_start_ms",
        "income_end_ms",
        "page_size",
        "income_pages",
        "balances",
        "positions",
    }:
        raise ValueError("complete recorded bundle required")
    if (
        type(bundle["schema_version"]) is not int
        or bundle["schema_version"] != 1
        or bundle["mode"] not in ("paper", "testnet")
    ):
        raise ValueError("recorded paper/testnet evidence required")
    server = integer(bundle["server_time_ms"])
    start, end = integer(bundle["income_start_ms"]), integer(bundle["income_end_ms"])
    if not start <= end <= server or end - start >= 604800000:
        raise ValueError("bounded common receipt cut required")
    mode = bundle["position_mode"]
    if mode not in ("ONE_WAY", "HEDGE"):
        raise ValueError("explicit position mode required")
    size, pages = bundle["page_size"], bundle["income_pages"]
    if (
        type(size) is not int
        or not 1 <= size <= 1000
        or type(pages) is not list
        or not 1 <= len(pages) <= 101
    ):
        raise ValueError("bounded enumeration required")
    receipts = []
    seen = set()
    for index, page in enumerate(pages):
        if (
            type(page) is not list
            or len(page) > size
            or (index < len(pages) - 1 and len(page) != size)
            or (index == len(pages) - 1 and len(page) >= size)
        ):
            raise ValueError("missing or inconsistent terminal page")
        for row in page:
            if type(row) is not dict:
                raise ValueError("income row required")
            kind = row["incomeType"]
            if type(kind) is not str or kind not in TYPES:
                raise ValueError("unsupported income type")
            if not start <= integer(row["time"]) <= end:
                raise ValueError("receipt outside declared window")
            key = "binance:" + kind + ":" + receipt_id(row["tranId"])
            if key in seen:
                raise ValueError("duplicate receipt across pages")
            seen.add(key)
            amount(row["income"])
            receipts.append(
                dict(receipt_id=key, currency=asset(row["asset"]), amount=row["income"])
            )
            if len(receipts) > 10000:
                raise ValueError("complete receipt bound exceeded")
    balances = []
    positions = []
    for group in ("balances", "positions"):
        if type(bundle[group]) is not list or len(bundle[group]) > 10000:
            raise ValueError("bounded account rows required")
    seen_assets = set()
    for row in bundle["balances"]:
        if type(row) is not dict:
            raise ValueError("balance row required")
        currency = asset(row["asset"])
        if currency in seen_assets:
            raise ValueError("duplicate asset")
        seen_assets.add(currency)
        amount(row["balance"])
        amount(row["availableBalance"])
        balances.append(
            dict(
                asset=currency, total=row["balance"], available=row["availableBalance"]
            )
        )
    seen_positions = set()
    for row in bundle["positions"]:
        if type(row) is not dict:
            raise ValueError("position row required")
        symbol = asset(row["symbol"])
        side = row["positionSide"]
        quantity = amount(row["positionAmt"])
        if type(side) is not str:
            raise ValueError("explicit position side required")
        key = (symbol, side)
        if key in seen_positions:
            raise ValueError("duplicate position identity including zero rows")
        seen_positions.add(key)
        if mode == "ONE_WAY":
            if side != "BOTH":
                raise ValueError("mixed position modes")
            normalized = "LONG" if quantity >= 0 else "SHORT"
        else:
            if (
                side not in ("LONG", "SHORT")
                or (side == "LONG" and quantity < 0)
                or (side == "SHORT" and quantity > 0)
            ):
                raise ValueError("inconsistent hedge side/sign")
            normalized = side
        if quantity != 0:
            positions.append(
                dict(
                    symbol=symbol,
                    side=normalized,
                    quantity=format(quantity.copy_abs(), "f"),
                )
            )
    cut = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=server)
    result = dict(
        schema_version=2,
        provider="binance_usdm",
        account=name(bundle["account"]),
        mode=bundle["mode"],
        as_of=cut.isoformat(),
        observed_at=bundle["observed_at"],
        complete=True,
        position_mode=mode,
        receipt_window=dict(start_ms=start, end_ms=end),
        balances=balances,
        positions=positions,
        receipts=receipts,
    )
    AccountSnapshot.parse(result, now=now)
    return result


def reconcile_recorded_account(expected, bundle, *, now=None):
    try:
        observed = adapt_recorded_account(bundle, now=now)
    except (ValueError, TypeError, KeyError, OverflowError):
        return dict(
            read_only=True,
            live_authorized=False,
            production_certified=False,
            status="unavailable",
            matched=False,
            differences=None,
        )
    return reconcile_accounts(expected, observed, now=now)


def main(argv=None):
    """Offline expected snapshot + recorded bundle; sanitized JSON result only."""
    import argparse
    import json
    from pathlib import Path
    from aitos.trading.account_reconciliation import read_snapshot_file

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("expected", type=Path)
    parser.add_argument("recorded_bundle", type=Path)
    args = parser.parse_args(argv)
    try:
        result = reconcile_recorded_account(
            read_snapshot_file(args.expected), read_snapshot_file(args.recorded_bundle)
        )
    except (ValueError, TypeError, OSError, RecursionError):
        result = reconcile_accounts(None, None)
    print(json.dumps(result, sort_keys=True))
    return (
        0
        if result["status"] == "matched"
        else 2 if result["status"] == "mismatch" else 3
    )


if __name__ == "__main__":
    raise SystemExit(main())
