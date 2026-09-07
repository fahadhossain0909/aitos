#!/usr/bin/env bash
set -euo pipefail

REDIS_CONTAINER="${AITOS_REDIS_CONTAINER:-aitos-redis}"
REPORT_DIR="${AITOS_FORENSIC_REPORT_DIR:-$HOME/aitos-market-data-e2e-forensics}"
REPORT="$REPORT_DIR/duplicates.json"
mkdir -p "$REPORT_DIR"

redis() { docker exec "$REDIS_CONTAINER" redis-cli --json "$@"; }

REDIS_CONTAINER="$REDIS_CONTAINER" REPORT="$REPORT" python3 - <<'PY'
import hashlib
import json
import os
import subprocess

container = os.environ["REDIS_CONTAINER"]
out = os.environ["REPORT"]


def redis_json(*args):
    raw = subprocess.check_output(
        ["docker", "exec", container, "redis-cli", "--json", *args],
        text=True,
        stderr=subprocess.DEVNULL,
    )
    return json.loads(raw or "null")


def scalar(v):
    if isinstance(v, (int, float, str)):
        return v
    return None


def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk(value)


def extract_trade(payload):
    candidates = list(walk(payload))
    symbol = None
    trade_id = None
    timestamp = None
    price = None
    quantity = None
    for item in candidates:
        if not isinstance(item, dict):
            continue
        symbol = symbol or item.get("symbol") or item.get("s")
        trade_id = trade_id if trade_id is not None else item.get("trade_id")
        trade_id = trade_id if trade_id is not None else item.get("a")
        trade_id = trade_id if trade_id is not None else item.get("id")
        timestamp = timestamp or item.get("timestamp") or item.get("T") or item.get("E")
        price = price if price is not None else item.get("price")
        price = price if price is not None else item.get("p")
        quantity = quantity if quantity is not None else item.get("quantity")
        quantity = quantity if quantity is not None else item.get("q")
    if symbol is None or trade_id is None:
        return None
    return {
        "symbol": str(symbol).upper(),
        "trade_id": str(trade_id),
        "timestamp": timestamp,
        "price": price,
        "quantity": quantity,
    }

streams = {}
for stream in ("stream:market.trade", "stream:market.book.snapshot", "stream:market.book.delta"):
    try:
        rows = redis_json("XRANGE", stream, "-", "+", "COUNT", "5000")
    except Exception:
        rows = []
    streams[stream] = rows if isinstance(rows, list) else []

trade_seen = {}
trade_duplicates = []
trade_unparsed = 0
for row in streams["stream:market.trade"]:
    if not isinstance(row, list) or len(row) != 2:
        continue
    entry_id, fields = row
    payload = {}
    if isinstance(fields, list):
        for i in range(0, len(fields) - 1, 2):
            payload[str(fields[i])] = fields[i + 1]
    parsed = None
    for value in payload.values():
        try:
            candidate = json.loads(value) if isinstance(value, str) else value
        except Exception:
            candidate = value
        parsed = extract_trade(candidate)
        if parsed:
            break
    if not parsed:
        trade_unparsed += 1
        continue
    key = (parsed["symbol"], parsed["trade_id"])
    fingerprint = hashlib.sha256(json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    previous = trade_seen.get(key)
    if previous:
        trade_duplicates.append({"key": key, "entry_id": entry_id, "previous": previous, "fingerprint": fingerprint})
    else:
        trade_seen[key] = {"entry_id": entry_id, "fingerprint": fingerprint}

result = {
    "status": "PASS" if not trade_duplicates else "FAIL",
    "trade_stream_entries_examined": len(streams["stream:market.trade"]),
    "trade_events_parsed": len(trade_seen),
    "trade_events_unparsed": trade_unparsed,
    "semantic_trade_duplicates": len(trade_duplicates),
    "duplicate_examples": trade_duplicates[:25],
    "note": "Binance USD-M aggTrade IDs are expected to be unique per symbol; this check detects repeated (symbol, trade_id) semantics inside the canonical Redis trade stream.",
}
json.dump(result, open(out, "w", encoding="utf-8"), indent=2, sort_keys=True)
print(json.dumps(result, indent=2, sort_keys=True))
if trade_duplicates:
    raise SystemExit(2)
PY
