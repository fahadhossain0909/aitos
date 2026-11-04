from datetime import datetime, timedelta, timezone

import asyncio

from aitos.intelligence.htf_cache import HigherTimeframeCandleCache
from aitos.models.market import Kline


def candle(close_time: datetime, *, closed: bool = True) -> Kline:
    return Kline(
        symbol="BTCUSDT",
        timeframe="1h",
        open_time=close_time - timedelta(hours=1),
        close_time=close_time,
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        volume=10.0,
        quote_volume=1010.0,
        trades_count=5,
        taker_buy_volume=6.0,
        taker_buy_quote_volume=606.0,
        is_closed=closed,
    )


def test_htf_cache_reuses_snapshot_until_next_candle_close():
    now = [datetime(2026, 11, 4, 13, 10, tzinfo=timezone.utc)]
    cache = HigherTimeframeCandleCache(clock=lambda: now[0])
    calls = []
    async def fetch():
        calls.append(1)
        return [candle(datetime(2026, 11, 4, 12, 59, 59, tzinfo=timezone.utc))]

    async def run():
        first, first_meta = await cache.get("BTCUSDT", "1h", 100, fetch)
        second, second_meta = await cache.get("BTCUSDT", "1h", 100, fetch)
        assert first == second
        assert first_meta["cache"] == "refreshed"
        assert second_meta["cache"] == "hit"
        assert len(calls) == 1

        now[0] = datetime(2026, 11, 4, 14, 1, tzinfo=timezone.utc)
        refreshed = candle(datetime(2026, 11, 4, 13, 59, 59, tzinfo=timezone.utc))
        async def fetch_next():
            calls.append(1)
            return [refreshed]
        third, meta = await cache.get("BTCUSDT", "1h", 100, fetch_next)
        assert third == [refreshed]
        assert meta["cache"] == "refreshed"
        assert len(calls) == 2

    asyncio.run(run())


def test_htf_cache_filters_forming_candles_and_preserves_last_good_snapshot():
    now = [datetime(2026, 11, 4, 13, 10, tzinfo=timezone.utc)]
    cache = HigherTimeframeCandleCache(clock=lambda: now[0])
    closed = candle(datetime(2026, 11, 4, 12, 59, 59, tzinfo=timezone.utc))

    async def run():
        first, _ = await cache.get(
            "BTCUSDT", "1h", 100, lambda: asyncio.sleep(0, result=[closed])
        )
        assert first == [closed]
        now[0] = datetime(2026, 11, 4, 14, 1, tzinfo=timezone.utc)
        forming = candle(
            datetime(2026, 11, 4, 13, 59, 59, tzinfo=timezone.utc),
            closed=False,
        )
        result, meta = await cache.get(
            "BTCUSDT", "1h", 100, lambda: asyncio.sleep(0, result=[forming])
        )
        assert result == [closed]
        assert meta["cache"] == "unchanged_fallback"

    asyncio.run(run())


def test_htf_cache_single_flight_for_concurrent_requests():
    now = datetime(2026, 11, 4, 13, 10, tzinfo=timezone.utc)
    cache = HigherTimeframeCandleCache(clock=lambda: now)
    calls = []

    async def fetch():
        calls.append(1)
        await asyncio.sleep(0.01)
        return [candle(datetime(2026, 11, 4, 12, 59, 59, tzinfo=timezone.utc))]

    async def run():
        results = await asyncio.gather(
            cache.get("BTCUSDT", "1h", 100, fetch),
            cache.get("BTCUSDT", "1h", 100, fetch),
        )
        assert results[0][0] == results[1][0]
        assert len(calls) == 1

    asyncio.run(run())
