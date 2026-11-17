"""Read-only Binance USD-M market microstructure observations for CFEI."""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from typing import Any
import aiohttp
from .contracts import CapitalEvent

class BinanceMarketStructureAdapter:
    """Collect public aggregate trades, OI, funding and depth snapshots.

    Large-trade thresholds are operator supplied quote-notional thresholds.
    Depth is a point-in-time snapshot, not an L3 order identity feed.
    """
    BASE_URL = "https://fapi.binance.com"

    def __init__(self, *, symbols: list[str], min_trade_notional_usd: float = 100_000,
                 timeout_seconds: float = 8.0, session: aiohttp.ClientSession | None = None):
        normalized = sorted({s.strip().upper() for s in symbols if s.strip()})
        if not normalized or len(normalized) > 50:
            raise ValueError("provide 1-50 symbols")
        if min_trade_notional_usd <= 0 or min_trade_notional_usd > 1e10:
            raise ValueError("invalid large-trade threshold")
        if not 0.2 <= timeout_seconds <= 30:
            raise ValueError("invalid timeout")
        self.symbols, self.threshold, self.timeout_seconds = normalized, float(min_trade_notional_usd), timeout_seconds
        self.session, self.owns_session = session, session is None

    async def close(self):
        if self.owns_session and self.session and not self.session.closed:
            await self.session.close()

    async def _get(self, path: str, params: dict[str, str]) -> Any:
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout_seconds))
        async with self.session.get(self.BASE_URL + path, params=params) as response:
            if response.status == 429:
                raise RuntimeError("Binance public API rate limit reached")
            response.raise_for_status()
            return await response.json()

    @staticmethod
    def _event_id(symbol: str, kind: str, identity: str) -> str:
        return hashlib.sha256(f"binance-usdm|{symbol}|{kind}|{identity}".encode()).hexdigest()

    async def poll(self, *, cursor: str | None = None) -> tuple[list[CapitalEvent], str | None]:
        now = datetime.now(timezone.utc)
        events: list[CapitalEvent] = []
        last_ids: dict[str, int] = {}
        if cursor:
            try:
                parsed = json.loads(cursor)
                if not isinstance(parsed, dict):
                    raise ValueError
                last_ids = {str(k): int(v) for k, v in parsed.items()}
            except (ValueError, TypeError, json.JSONDecodeError):
                raise ValueError("cursor must be a JSON object of symbol to aggregate-trade id") from None
        for symbol in self.symbols:
            params = {"symbol": symbol, "limit": "1000"}
            if symbol in last_ids:
                params["fromId"] = str(last_ids[symbol] + 1)
            trades = await self._get("/fapi/v1/aggTrades", params)
            if isinstance(trades, list) and trades:
                last_ids[symbol] = max(int(row["a"]) for row in trades)
            for trade in trades if isinstance(trades, list) else []:
                stamp_ms = int(trade["T"])
                notional = float(trade["p"]) * float(trade["q"])
                if notional < self.threshold:
                    continue
                stamp = datetime.fromtimestamp(stamp_ms / 1000, timezone.utc)
                events.append(CapitalEvent(
                    self._event_id(symbol, "large_aggregate_trade", str(trade["a"])),
                    "binance-usdm-market", "exchange_public_trade", "crypto_futures",
                    "binance_usdm", symbol, "large_aggregate_trade", stamp, now,
                    float(trade["q"]), "base_asset", notional,
                    transaction_id=str(trade["a"]), confidence=1.0,
                    metadata={"price": str(trade["p"]), "buyer_is_maker": bool(trade["m"]),
                              "aggressor_side": "sell" if trade["m"] else "buy",
                              "aggregate_trade": True, "individual_identity_available": False}))
            oi = await self._get("/fapi/v1/openInterest", {"symbol": symbol})
            events.append(CapitalEvent(
                self._event_id(symbol, "open_interest_snapshot", str(oi["time"])),
                "binance-usdm-market", "exchange_public_metric", "crypto_futures",
                "binance_usdm", symbol, "open_interest_snapshot",
                datetime.fromtimestamp(int(oi["time"])/1000, timezone.utc), now,
                float(oi["openInterest"]), "base_asset", None,
                metadata={"metric": "open_interest", "raw": oi}))
            premium = await self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
            funding_time = int(premium["nextFundingTime"])
            events.append(CapitalEvent(
                self._event_id(symbol, "funding_snapshot", str(premium["time"])),
                "binance-usdm-market", "exchange_public_metric", "crypto_futures",
                "binance_usdm", symbol, "funding_snapshot",
                datetime.fromtimestamp(int(premium["time"])/1000, timezone.utc), now,
                abs(float(premium["lastFundingRate"])), "funding_rate", None,
                metadata={"funding_rate": str(premium["lastFundingRate"]),
                          "mark_price": str(premium["markPrice"]),
                          "index_price": str(premium["indexPrice"]),
                          "next_funding_time_ms": funding_time}))
            depth = await self._get("/fapi/v1/depth", {"symbol": symbol, "limit": "100"})
            bids = [(float(p), float(q)) for p, q in depth.get("bids", [])]
            asks = [(float(p), float(q)) for p, q in depth.get("asks", [])]
            bid_notional = sum(p*q for p, q in bids)
            ask_notional = sum(p*q for p, q in asks)
            total = bid_notional + ask_notional
            imbalance = (bid_notional - ask_notional) / total if total else 0.0
            events.append(CapitalEvent(
                self._event_id(symbol, "depth_snapshot", str(depth["lastUpdateId"])),
                "binance-usdm-market", "exchange_public_metric", "crypto_futures",
                "binance_usdm", symbol, "depth_snapshot",
                now, now, total, "usd_notional", None,
                metadata={"bid_notional": bid_notional, "ask_notional": ask_notional,
                          "depth_imbalance": imbalance, "last_update_id": depth["lastUpdateId"],
                          "depth_limit": 100, "is_l3": False}))
        return events, json.dumps(last_ids, sort_keys=True, separators=(",", ":"))
