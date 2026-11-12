from datetime import datetime, timedelta, timezone

import pytest

from aitos.intelligence.historical_market_intelligence import fetch_historical_klines
from aitos.models.market import Kline


def _row(index, opened):
    close = 100.0 + index
    return {
        "time": opened,
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 10.0,
        "quote_volume": 1000.0,
        "trades_count": 10,
        "taker_buy_volume": 5.0,
        "taker_buy_quote_volume": 500.0,
    }


class Repository:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def get_recent_klines(self, symbol, timeframe, limit):
        self.calls.append((symbol, timeframe, limit))
        return self.rows[-limit:]


def _kline(index, opened):
    close = 100.0 + index
    return Kline(
        "BTCUSDT",
        "1m",
        opened,
        opened + timedelta(minutes=1),
        close,
        close + 1,
        close - 1,
        close,
        10.0,
        1000.0,
        10,
        5.0,
        500.0,
    )


@pytest.mark.asyncio
async def test_clickhouse_history_is_used_before_rest():
    now = datetime.now(timezone.utc)
    rows = [_row(i, now - timedelta(minutes=60 - i)) for i in range(60)]
    repository = Repository(rows)
    rest_calls = 0

    async def rest_fetcher(*args, **kwargs):
        nonlocal rest_calls
        rest_calls += 1
        return []

    result = await fetch_historical_klines(
        "BTCUSDT",
        "1m",
        limit=60,
        exchange_fetcher=rest_fetcher,
        repository=repository,
        minimum_history=53,
    )
    assert len(result) == 60
    assert result[0].open_time < result[-1].open_time
    assert rest_calls == 0
    assert repository.calls == [("BTCUSDT", "1m", 60)]


@pytest.mark.asyncio
async def test_insufficient_clickhouse_history_uses_rest_and_merges():
    now = datetime.now(timezone.utc)
    stored = [_row(i, now - timedelta(minutes=20 - i)) for i in range(20)]
    repository = Repository(stored)
    remote = [_kline(i, now - timedelta(minutes=80 - i)) for i in range(60)]
    calls = 0

    async def rest_fetcher(symbol, timeframe, limit):
        nonlocal calls
        calls += 1
        return remote[-limit:]

    result = await fetch_historical_klines(
        "BTCUSDT",
        "1m",
        limit=60,
        exchange_fetcher=rest_fetcher,
        repository=repository,
        minimum_history=53,
    )
    assert calls == 1
    assert len(result) == 60
    assert result[-1].open_time > remote[-1].open_time
