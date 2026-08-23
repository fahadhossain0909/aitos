"""Exchange adapter contract.

Every venue (Binance today; others later) implements this so the rest of
AITOS — the ingestion service, agents, backtester — never imports
venue-specific code directly. Swap adapters, nothing upstream changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, List, Optional

from aitos.models.market import (FundingRate, Kline, OpenInterest,
                                 OrderBookSnapshot, TradeTick)


class ExchangeAdapter(ABC):
    """Async context-managed adapter for a single exchange/market (e.g. Binance USDT-M Futures)."""

    async def __aenter__(self) -> "ExchangeAdapter":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    @abstractmethod
    async def connect(self) -> None:
        """Open underlying HTTP session(s). Idempotent."""

    @abstractmethod
    async def close(self) -> None:
        """Close underlying HTTP session(s) and any open streams."""

    # -- REST (point-in-time / backfill) --------------------------------------

    @abstractmethod
    async def fetch_klines(
        self, symbol: str, timeframe: str, limit: int = 500
    ) -> List[Kline]:
        """Fetch the most recent ``limit`` closed candles for symbol/timeframe."""

    @abstractmethod
    async def fetch_order_book(self, symbol: str, limit: int = 50) -> OrderBookSnapshot:
        """Fetch a current order book depth snapshot."""

    @abstractmethod
    async def fetch_recent_trades(
        self, symbol: str, limit: int = 500
    ) -> List[TradeTick]:
        """Fetch the most recent executed trades."""

    @abstractmethod
    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        """Fetch the current/last funding rate and mark price."""

    @abstractmethod
    async def fetch_open_interest(self, symbol: str) -> OpenInterest:
        """Fetch the current open interest."""

    async def fetch_exchange_info(self, symbols: Optional[List[str]] = None) -> "dict":
        """Fetch per-symbol trading filters (quantity step, price tick,
        minimum notional). Concrete (not abstract) with a default that
        raises, since not every adapter/test double needs to support it —
        override in adapters that do (e.g. ``BinanceFuturesAdapter``)."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support fetch_exchange_info"
        )

    # -- Streaming (live) -------------------------------------------------------

    @abstractmethod
    def stream_klines(self, symbols: List[str], timeframe: str) -> AsyncIterator[Kline]:
        """Yield klines as they update/close, for all ``symbols``."""

    @abstractmethod
    def stream_trades(self, symbols: List[str]) -> AsyncIterator[TradeTick]:
        """Yield trade ticks as they execute, for all ``symbols``."""

    @abstractmethod
    def stream_order_book(self, symbols: List[str]) -> AsyncIterator[OrderBookSnapshot]:
        """Yield order book updates, for all ``symbols``."""
