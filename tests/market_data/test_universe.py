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

    with pytest.raises(
        RuntimeError, match="no eligible, trusted USDT perpetual symbols"
    ):
        await resolve_live_universe(EmptyExchange())


@pytest.mark.asyncio
async def test_resolve_live_universe_requires_valid_contract_metadata_for_non_core():
    class Metadata:
        def __init__(self, status, contract_type, quote_asset):
            self.status = status
            self.contract_type = contract_type
            self.quote_asset = quote_asset

    class Exchange:
        async def fetch_exchange_info(self, symbols=None):
            return {
                "BTCUSDT": Metadata("TRADING", "PERPETUAL", "USDT"),
                "SCAMUSDT": Metadata("TRADING", "CURRENT_QUARTER", "USDT"),
                "DELISTUSDT": Metadata("BREAK", "PERPETUAL", "USDT"),
                "BADQUOTEUSDT": Metadata("TRADING", "PERPETUAL", "BUSD"),
            }

        async def fetch_24h_tickers(self):
            return [
                {"symbol": "BTCUSDT", "quoteVolume": "30000000000", "count": 500000}
            ]

    assert await resolve_live_universe(Exchange()) == ["BTCUSDT"]


@pytest.mark.asyncio
async def test_resolve_live_universe_adds_only_liquid_dynamic_perpetuals():
    class Metadata:
        status = "TRADING"
        contract_type = "PERPETUAL"
        quote_asset = "USDT"
        onboard_date = 0

    class Exchange:
        async def fetch_exchange_info(self, symbols=None):
            return {
                "BTCUSDT": Metadata(),
                "ALPHAUSDT": Metadata(),
                "BETAUSDT": Metadata(),
            }

        async def fetch_24h_tickers(self):
            return [
                {"symbol": "BTCUSDT", "quoteVolume": "30000000000", "count": 500000},
                {"symbol": "ALPHAUSDT", "quoteVolume": "25000000", "count": 30000},
                {"symbol": "BETAUSDT", "quoteVolume": "1000000", "count": 5000},
            ]

    assert await resolve_live_universe(Exchange()) == ["BTCUSDT", "ALPHAUSDT"]
