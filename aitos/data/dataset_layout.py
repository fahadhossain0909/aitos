"""Canonical on-disk layout for ProjectAlpha historical market data.

High-volume L2 order-book snapshots and incremental updates are kept in
separate datasets. Other market data has its own logical datasets so readers
can load only what a backtest needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class DatasetLayout:
    root: Path

    def _partition(
        self, dataset: str, exchange: str, market: str, symbol: str, day: date
    ) -> Path:
        return (
            self.root
            / dataset
            / f"exchange={exchange}"
            / f"market={market}"
            / f"symbol={symbol.upper()}"
            / f"date={day.isoformat()}"
        )

    def trades(self, exchange: str, market: str, symbol: str, day: date) -> Path:
        return self._partition("trades", exchange, market, symbol, day)

    def prices(self, exchange: str, market: str, symbol: str, day: date) -> Path:
        return self._partition("prices", exchange, market, symbol, day)

    def funding(self, exchange: str, market: str, symbol: str, day: date) -> Path:
        return self._partition("funding", exchange, market, symbol, day)

    def open_interest(self, exchange: str, market: str, symbol: str, day: date) -> Path:
        return self._partition("open_interest", exchange, market, symbol, day)

    def liquidations(self, exchange: str, market: str, symbol: str, day: date) -> Path:
        return self._partition("liquidations", exchange, market, symbol, day)

    def orderbook_snapshots(
        self, exchange: str, market: str, symbol: str, day: date
    ) -> Path:
        return self._partition("orderbook/snapshots", exchange, market, symbol, day)

    def orderbook_updates(
        self, exchange: str, market: str, symbol: str, day: date
    ) -> Path:
        return self._partition("orderbook/updates", exchange, market, symbol, day)

    @staticmethod
    def is_complete_partition(path: Path) -> bool:
        return path.is_dir() and any(path.glob("*.parquet"))
