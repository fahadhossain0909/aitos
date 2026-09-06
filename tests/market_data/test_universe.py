import pytest

from aitos.market_data.universe import resolve_live_universe


class FakeExchange:
    async def fetch_exchange_info(self, symbols=None):
        return {
            "BTCUSDT": object(),
            "ETHUSDT": object(),
            "SOLUSDT": object(),
            "BTCUSD": object(),
            "ETHBTC": object(),
        }


@pytest.mark.asyncio
async def test_resolve_live_universe_returns_all_usdt_symbols():
    symbols = await resolve_live_universe(FakeExchange())
    assert symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


@pytest.mark.asyncio
async def test_resolve_live_universe_fails_when_empty():
    class EmptyExchange:
        async def fetch_exchange_info(self, symbols=None):
            return {}

    with pytest.raises(RuntimeError, match="no USDT trading symbols"):
        await resolve_live_universe(EmptyExchange())
