from datetime import datetime, timedelta, timezone

import pytest

from aitos.capital_intelligence.valuation import PriceQuote, value_quantity


class PriceBook:
    def __init__(self, quote):
        self._quote = quote

    async def quote(self, asset_id, at):
        return self._quote


@pytest.mark.asyncio
async def test_point_in_time_valuation_rejects_future_quote():
    now = datetime.now(timezone.utc)
    quote = PriceQuote("BTC", "USD", 50_000, now + timedelta(seconds=1), "fixture")
    assert await value_quantity(PriceBook(quote), "BTC", 1, at=now) is None


@pytest.mark.asyncio
async def test_point_in_time_valuation_returns_value_and_provenance():
    now = datetime.now(timezone.utc)
    quote = PriceQuote("BTC", "USD", 50_000, now, "fixture")
    result = await value_quantity(PriceBook(quote), "BTC", 2, at=now)
    assert result[0] == 100_000
    assert result[1] == quote
