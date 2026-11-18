"""Read-only Binance USD-M Futures aggregate positioning collector."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .contracts import CapitalEvent


class BinanceFuturesCapitalAdapter:
    BASE_URL = "https://fapi.binance.com"

    def __init__(
        self,
        *,
        symbols: list[str],
        timeout_seconds: float = 8.0,
        session: aiohttp.ClientSession | None = None,
    ):
        normalized = sorted({s.strip().upper() for s in symbols if s.strip()})
        if not normalized or len(normalized) > 100:
            raise ValueError("provide 1-100 symbols")
        if not 0.2 <= timeout_seconds <= 30:
            raise ValueError("invalid timeout")
        self.symbols = normalized
        self.timeout_seconds = timeout_seconds
        self.session = session
        self.owns_session = session is None

    async def close(self):
        if self.owns_session and self.session and not self.session.closed:
            await self.session.close()

    async def _get(self, path: str, params: dict[str, str]) -> Any:
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds)
            )
        async with self.session.get(self.BASE_URL + path, params=params) as response:
            if response.status == 429:
                raise RuntimeError("Binance public API rate limit reached")
            response.raise_for_status()
            return await response.json()

    async def poll(
        self, *, cursor: str | None = None
    ) -> tuple[list[CapitalEvent], str | None]:
        now = datetime.now(timezone.utc)
        events = []
        watermark = None
        if cursor:
            try:
                watermark = datetime.fromisoformat(cursor).timestamp() - 300
            except ValueError:
                raise ValueError("cursor must be an ISO timestamp") from None
        for symbol in self.symbols:
            for endpoint, kind in (
                (
                    "/futures/data/topLongShortPositionRatio",
                    "top_trader_position_ratio",
                ),
                ("/futures/data/topLongShortAccountRatio", "top_trader_account_ratio"),
                ("/futures/data/takerlongshortRatio", "taker_buy_sell_ratio"),
            ):
                rows = await self._get(
                    endpoint, {"symbol": symbol, "period": "5m", "limit": "30"}
                )
                for row in rows if isinstance(rows, list) else []:
                    stamp = datetime.fromtimestamp(
                        int(row["timestamp"]) / 1000, timezone.utc
                    )
                    if watermark is not None and stamp.timestamp() < watermark:
                        continue
                    event_id = hashlib.sha256(
                        f"{symbol}|{endpoint}|{row['timestamp']}".encode()
                    ).hexdigest()
                    values = {
                        k: v for k, v in row.items() if k not in {"symbol", "timestamp"}
                    }
                    events.append(
                        CapitalEvent(
                            event_id,
                            "binance-usdm-public",
                            "exchange_aggregate",
                            "crypto_futures",
                            "binance_usdm",
                            symbol,
                            kind,
                            stamp,
                            now,
                            0,
                            "ratio",
                            None,
                            confidence=1.0,
                            metadata={
                                "aggregate_only": True,
                                "individual_identity_available": False,
                                "values": values,
                            },
                        )
                    )
        return events, now.isoformat()
