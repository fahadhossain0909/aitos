from datetime import datetime, timezone
import pytest

from aitos.capital_intelligence.binance_price import BinanceHistoricalPriceBook
from aitos.capital_intelligence.token_registry import TokenMetadata


class Response:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def raise_for_status(self):
        return None

    async def json(self):
        return self.rows


class Session:
    def __init__(self, rows):
        self.rows = rows
        self.params = None

    def get(self, url, params):
        self.params = params
        return Response(self.rows)


@pytest.mark.asyncio
async def test_binance_price_book_uses_only_closed_candles_at_or_before_event():
    token = TokenMetadata("1", "0x" + "a" * 40, "asset:btc", "BTC", 8, "BTCUSDT")
    at = datetime.fromtimestamp(120_000 / 1000, timezone.utc)
    session = Session(
        [
            [0, "0", "0", "0", "99", "0", 59_999],
            [60_000, "0", "0", "0", "100", "0", 119_999],
            [120_000, "0", "0", "0", "101", "0", 179_999],
        ]
    )
    book = BinanceHistoricalPriceBook([token], session=session)
    quote = await book.quote("asset:btc", at)
    assert quote.price == 100
    assert quote.observed_at.timestamp() == 119.999
    assert quote.quote_currency == "USDT"
    assert session.params["endTime"] == "120000"


@pytest.mark.asyncio
async def test_binance_price_book_returns_none_when_only_future_close_exists():
    token = TokenMetadata("1", "0x" + "a" * 40, "asset:btc", "BTC", 8, "BTCUSDT")
    at = datetime.fromtimestamp(120_000 / 1000, timezone.utc)
    session = Session([[120_000, "0", "0", "0", "101", "0", 179_999]])
    book = BinanceHistoricalPriceBook([token], session=session)
    assert await book.quote("asset:btc", at) is None
