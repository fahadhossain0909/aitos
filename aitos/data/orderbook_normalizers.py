"""Normalize common exchange order-book CSV layouts into CanonicalBookEvent."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .schema import CanonicalBookEvent, normalize_timestamp


def _ts(value: str) -> datetime:
    if value.isdigit():
        n = int(value)
        return datetime.fromtimestamp(
            n / (1_000_000 if n >= 10**14 else 1_000), tz=timezone.utc
        )
    return normalize_timestamp(value)


def _side(value: str) -> str:
    value = value.strip().lower()
    if value in {"bid", "b", "buy"}:
        return "buy"
    if value in {"ask", "a", "sell"}:
        return "sell"
    raise ValueError(f"unknown book side: {value}")


def iter_orderbook_csv(
    path: str | Path, symbol: str, exchange: str, market: str
) -> Iterator[CanonicalBookEvent]:
    """Read normalized/event-style CSV files with side/price/quantity columns."""
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            row = {
                str(k).strip().lower(): str(v).strip()
                for k, v in raw.items()
                if k is not None
            }
            side = row.get("side") or row.get("type") or row.get("action_side")
            price = row.get("price")
            qty = row.get("quantity") or row.get("qty") or row.get("size")
            ts = row.get("timestamp") or row.get("time") or row.get("ts")
            update_id = (
                row.get("update_id")
                or row.get("updateid")
                or row.get("u")
                or row.get("id")
            )
            if not all((side, price, qty, ts, update_id)):
                raise ValueError(
                    "order-book row requires side, price, quantity, timestamp and update_id"
                )
            yield CanonicalBookEvent(
                exchange,
                market,
                symbol.upper(),
                update_id,
                _ts(ts),
                _side(side),
                float(price),
                float(qty),
            )


def iter_binance_depth_jsonl(
    path: str | Path, symbol: str, market: str = "futures_um"
) -> Iterator[CanonicalBookEvent]:
    """Read Binance depth-update JSONL: bids/asks arrays become individual events."""
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            msg: dict[str, Any] = json.loads(line)
            data = msg.get("data", msg)
            update_id = data.get("u") or data.get("U") or data.get("lastUpdateId")
            raw_ts = data.get("E") or data.get("T") or data.get("timestamp")
            if update_id is None or raw_ts is None:
                continue
            timestamp = _ts(str(raw_ts))
            for side_key, side in (
                ("b", "buy"),
                ("a", "sell"),
                ("bids", "buy"),
                ("asks", "sell"),
            ):
                for level in data.get(side_key, []) or []:
                    price, qty = level[0], level[1]
                    yield CanonicalBookEvent(
                        "binance",
                        market,
                        symbol.upper(),
                        update_id,
                        timestamp,
                        side,
                        float(price),
                        float(qty),
                    )


def iter_bybit_depth_jsonl(
    path: str | Path, symbol: str, market: str = "linear"
) -> Iterator[CanonicalBookEvent]:
    """Read Bybit orderbook JSONL messages with ``data.b``/``data.a`` levels."""
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            msg: dict[str, Any] = json.loads(line)
            data = msg.get("data", msg)
            update_id = data.get("u") or data.get("seq") or data.get("update_id")
            raw_ts = msg.get("ts") or data.get("ts") or data.get("timestamp")
            if update_id is None or raw_ts is None:
                continue
            timestamp = _ts(str(raw_ts))
            for side_key, side in (
                ("b", "buy"),
                ("a", "sell"),
                ("bids", "buy"),
                ("asks", "sell"),
            ):
                for level in data.get(side_key, []) or []:
                    price, qty = level[0], level[1]
                    yield CanonicalBookEvent(
                        "bybit",
                        market,
                        symbol.upper(),
                        update_id,
                        timestamp,
                        side,
                        float(price),
                        float(qty),
                    )
