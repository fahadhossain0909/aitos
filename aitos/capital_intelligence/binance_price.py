"""Historical Binance spot kline quotes with candle-close no-lookahead semantics."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiohttp

from .token_registry import TokenMetadata
from .valuation import PriceQuote


class BinanceHistoricalPriceBook:
    BASE_URL = "https://api.binance.com"

    def __init__(
        self,
        tokens: list[TokenMetadata],
        *,
        timeout_seconds: float = 8.0,
        session: aiohttp.ClientSession | None = None,
    ):
        if not tokens or len(tokens) > 1000:
            raise ValueError("provide 1-1000 registered tokens")
        if not 0.2 <= timeout_seconds <= 30:
            raise ValueError("invalid timeout")
        self.tokens = {token.asset_id: token for token in tokens if token.price_symbol}
        if not self.tokens:
            raise ValueError("at least one token needs a configured price symbol")
        self.timeout_seconds = timeout_seconds
        self.session = session
        self.owns_session = session is None

    async def close(self) -> None:
        if self.owns_session and self.session and not self.session.closed:
            await self.session.close()

    async def quote(self, asset_id: str, at: datetime) -> PriceQuote | None:
        if at.tzinfo is None:
            raise ValueError("point-in-time request must be timezone-aware")
        token = self.tokens.get(asset_id)
        if token is None:
            return None
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds)
            )
        at_utc = at.astimezone(timezone.utc)
        at_ms = int(at_utc.timestamp() * 1000)
        async with self.session.get(
            self.BASE_URL + "/api/v3/klines",
            params={
                "symbol": token.price_symbol,
                "interval": "1m",
                "startTime": str(max(0, at_ms - 180_000)),
                "endTime": str(at_ms),
                "limit": "3",
            },
        ) as response:
            if response.status == 429:
                raise RuntimeError("Binance spot kline rate limit reached")
            response.raise_for_status()
            rows: Any = await response.json()
        eligible = [
            row
            for row in rows
            if len(row) > 6 and int(row[6]) <= at_ms
        ]
        if not eligible:
            return None
        row = max(eligible, key=lambda item: int(item[6]))
        close_time = datetime.fromtimestamp(int(row[6]) / 1000, timezone.utc)
        return PriceQuote(
            asset_id=asset_id,
            quote_currency=token.price_quote_currency,
            price=float(row[4]),
            observed_at=close_time,
            source=f"binance_spot_klines:{token.price_symbol}:1m",
        )
