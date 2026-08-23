"""Normalize common Binance/Bybit trade CSV layouts into canonical events."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .schema import CanonicalTrade, normalize_timestamp


def _number(value: str) -> float:
    return float(value)


def _timestamp(value: str) -> datetime:
    if value.isdigit():
        n = int(value)
        # Exchange files commonly use milliseconds; accept microseconds too.
        if n >= 10**14:
            return datetime.fromtimestamp(n / 1_000_000, tz=timezone.utc)
        return datetime.fromtimestamp(n / 1_000, tz=timezone.utc)
    return normalize_timestamp(value)


def _side_from_maker_flag(value: str) -> tuple[str, bool]:
    maker = value.strip().lower() in {"true", "1", "yes"}
    # Binance buyer-maker=true means the buyer was the passive side; the
    # aggressor was therefore a seller.
    return ("sell" if maker else "buy", maker)


def iter_binance_aggtrades(
    path: str | Path, symbol: str, exchange: str = "binance", market: str = "futures_um"
) -> Iterator[CanonicalTrade]:
    """Read Binance aggTrades CSV with or without a header."""
    path = Path(path)
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        first = next(reader, None)
        if first is None:
            return
        header = [x.strip().lower() for x in first]
        has_header = "price" in header and ("qty" in header or "quantity" in header)
        if has_header:
            rows = reader
            index = {name: i for i, name in enumerate(header)}
            get = lambda row, *names: row[next(index[n] for n in names if n in index)]
        else:
            rows = iter([first], reader)
            get = lambda row, *names: row[
                {
                    "agg_trade_id": 0,
                    "price": 1,
                    "qty": 2,
                    "quantity": 2,
                    "first_trade_id": 3,
                    "last_trade_id": 4,
                    "timestamp": 5,
                    "is_buyer_maker": 6,
                }[next(names)]
            ]

        for row in rows:
            trade_id = get(row, "agg_trade_id", "trade_id")
            price = _number(get(row, "price"))
            quantity = _number(get(row, "qty", "quantity"))
            ts = _timestamp(get(row, "timestamp", "time"))
            side, maker = _side_from_maker_flag(
                get(row, "is_buyer_maker", "buyer_maker")
            )
            yield CanonicalTrade(
                exchange,
                market,
                symbol.upper(),
                str(trade_id),
                ts,
                price,
                quantity,
                side,
                maker,
            )


def iter_bybit_trades(
    path: str | Path, symbol: str, exchange: str = "bybit", market: str = "spot"
) -> Iterator[CanonicalTrade]:
    """Read common Bybit public trade CSV variants by column name."""
    path = Path(path)
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            normalized = {
                str(k).strip().lower(): str(v).strip()
                for k, v in row.items()
                if k is not None
            }
            trade_id = (
                normalized.get("tradeid")
                or normalized.get("trade_id")
                or normalized.get("id")
                or ""
            )
            price = _number(normalized.get("price", "0"))
            quantity = _number(
                normalized.get("size")
                or normalized.get("qty")
                or normalized.get("quantity")
                or "0"
            )
            raw_side = (normalized.get("side") or "").lower()
            side = "buy" if raw_side in {"buy", "b"} else "sell"
            raw_ts = (
                normalized.get("timestamp")
                or normalized.get("time")
                or normalized.get("ts")
            )
            if raw_ts is None:
                raise ValueError("Bybit trade row has no timestamp column")
            yield CanonicalTrade(
                exchange,
                market,
                symbol.upper(),
                trade_id,
                _timestamp(raw_ts),
                price,
                quantity,
                side,
                None,
            )
