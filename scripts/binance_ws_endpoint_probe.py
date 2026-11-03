#!/usr/bin/env python3
import asyncio
import json
import os
import time
from urllib.parse import quote

import websockets

SYMBOL = os.getenv("BINANCE_PROBE_SYMBOL", "btcusdt").lower()
STREAM = f"{SYMBOL}@aggTrade"
BASE = "wss://fstream.binance.com"

CANDIDATES = [
    ("combined", f"{BASE}/stream?streams={quote(STREAM, safe='@')}"),
    ("raw", f"{BASE}/ws/{STREAM}"),
    ("market_combined", f"{BASE}/market/stream?streams={quote(STREAM, safe='@')}"),
    ("market_raw", f"{BASE}/market/ws/{STREAM}"),
]


async def probe(name, url):
    started = time.monotonic()
    out = {"name": name, "url": url}
    try:
        async with websockets.connect(
            url,
            open_timeout=10,
            ping_interval=15,
            ping_timeout=10,
            close_timeout=3,
            max_size=4 * 1024 * 1024,
        ) as ws:
            out["handshake_ms"] = round((time.monotonic() - started) * 1000, 1)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                timeout = max(0.1, deadline - time.monotonic())
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    break
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", "replace")
                try:
                    payload = json.loads(msg)
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    data = payload.get("data", payload)
                    if isinstance(data, dict) and data.get("e") == "aggTrade":
                        out["first_message_ms"] = round(
                            (time.monotonic() - started) * 1000, 1
                        )
                        out["event"] = data.get("e")
                        out["symbol"] = data.get("s")
                        out["trade_id"] = data.get("a")
                        out["ok"] = True
                        return out
                out.setdefault("non_trade_messages", 0)
                out["non_trade_messages"] += 1
            out["ok"] = False
            out["error"] = "no_aggTrade_before_timeout"
    except Exception as exc:
        out["ok"] = False
        out["error_type"] = type(exc).__name__
        out["error"] = str(exc)
    return out


async def main():
    print(
        json.dumps(
            {"endpoint_probe": [await probe(n, u) for n, u in CANDIDATES]}, indent=2
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
