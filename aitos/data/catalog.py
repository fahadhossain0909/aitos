"""Historical public-data URL catalogs for Binance and Bybit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import quote


@dataclass(frozen=True)
class RemoteFile:
    exchange: str
    market: str
    symbol: str
    date: date
    url: str
    filename: str


def _days(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def binance_um_daily_aggtrades(symbol: str, start: date, end: date) -> list[RemoteFile]:
    symbol = symbol.upper()
    result = []
    for day in _days(start, end):
        filename = f"{symbol}-aggTrades-{day:%Y-%m-%d}.zip"
        url = f"https://data.binance.vision/data/futures/um/daily/aggTrades/{quote(symbol)}/{quote(filename)}"
        result.append(RemoteFile("binance", "futures_um", symbol, day, url, filename))
    return result


def bybit_spot_daily_trades(symbol: str, start: date, end: date) -> list[RemoteFile]:
    symbol = symbol.upper()
    result = []
    for day in _days(start, end):
        filename = f"{symbol}_{day:%Y-%m-%d}.csv.gz"
        url = f"https://public.bybit.com/spot/{quote(symbol)}/{quote(filename)}"
        result.append(RemoteFile("bybit", "spot", symbol, day, url, filename))
    return result
