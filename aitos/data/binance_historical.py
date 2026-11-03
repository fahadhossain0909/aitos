"""Binance public historical-data ingestion helpers.

The module intentionally contains no API-key dependency. It only builds the
public archive URLs and maps archive types to ProjectAlpha datasets; the
existing incremental downloader performs the actual HTTP transfer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

PUBLIC_BASE = "https://data.binance.vision/data"


@dataclass(frozen=True)
class BinanceArchive:
    dataset: str
    url: str
    filename: str


def _filename(kind: str, symbol: str, day: date, interval: str | None = None) -> str:
    suffix = f"-{interval}" if interval else ""
    return f"{symbol.upper()}-{kind}{suffix}-{day.isoformat()}.zip"


def trade_archive(symbol: str, market: str, day: date) -> BinanceArchive:
    if market != "futures_um":
        raise ValueError("Only Binance USD-M futures is currently mapped")
    name = _filename("trades", symbol, day)
    url = f"{PUBLIC_BASE}/futures/um/daily/trades/{symbol.upper()}/{name}"
    return BinanceArchive("trades", url, name)


def agg_trade_archive(symbol: str, market: str, day: date) -> BinanceArchive:
    if market != "futures_um":
        raise ValueError("Only Binance USD-M futures is currently mapped")
    name = _filename("aggTrades", symbol, day)
    url = f"{PUBLIC_BASE}/futures/um/daily/aggTrades/{symbol.upper()}/{name}"
    return BinanceArchive("trades", url, name)


def book_depth_archive(
    symbol: str, market: str, day: date, interval: str = "100ms"
) -> BinanceArchive:
    """Return the Binance depth archive descriptor.

    This is kept separate from normalized L2 snapshots/updates because the
    archive format is exchange-specific and must be normalized before writing.
    """
    if market != "futures_um":
        raise ValueError("Only Binance USD-M futures is currently mapped")
    name = _filename("depth", symbol, day, interval)
    url = f"{PUBLIC_BASE}/futures/um/daily/bookDepth/{symbol.upper()}/{name}"
    return BinanceArchive("orderbook_updates", url, name)


def daily_archives(symbol: str, market: str, day: date) -> list[BinanceArchive]:
    """Return the currently supported public archives for one symbol/day."""
    return [trade_archive(symbol, market, day), book_depth_archive(symbol, market, day)]
