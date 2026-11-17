from datetime import datetime, timedelta, timezone
import pytest

from aitos.capital_intelligence.contracts import CapitalEvent
from aitos.capital_intelligence.enrichment import enrich_capital_event
from aitos.capital_intelligence.token_registry import TokenMetadata, TokenRegistry
from aitos.capital_intelligence.valuation import PriceQuote


class PriceBook:
    def __init__(self, quote):
        self._quote = quote

    async def quote(self, asset_id, at):
        return self._quote


@pytest.mark.asyncio
async def test_enrichment_uses_registry_decimals_and_records_usdt_proxy():
    now = datetime.now(timezone.utc)
    token = TokenMetadata("1", "0x" + "a" * 40, "asset:usdc", "USDC", 6, "USDCUSDT")
    event = CapitalEvent(
        "e1",
        "evm:1",
        "onchain_transfer",
        "crypto_spot",
        "1",
        token.address,
        "erc20_transfer",
        now,
        now,
        1234567,
        "token_base_units",
        None,
        actor_id="wallet:a",
        metadata={"raw_amount": "1234567", "flow_sign": 1},
    )
    quote = PriceQuote("asset:usdc", "USDT", 1.0, now, "fixture")
    result = await enrich_capital_event(
        event,
        registry=TokenRegistry([token]),
        price_book=PriceBook(quote),
        allow_usdt_usd_proxy=True,
    )
    assert result.quantity == 1.234567
    assert result.usd_value == 1.234567
    assert "USDT proxy" in result.metadata["usd_valuation_basis"]


@pytest.mark.asyncio
async def test_enrichment_rejects_future_price_and_keeps_usd_unset():
    now = datetime.now(timezone.utc)
    token = TokenMetadata("1", "0x" + "a" * 40, "asset:usdc", "USDC", 6, "USDCUSDT")
    event = CapitalEvent(
        "e1",
        "evm:1",
        "onchain_transfer",
        "crypto_spot",
        "1",
        token.address,
        "erc20_transfer",
        now,
        now,
        1000000,
        "token_base_units",
        None,
        actor_id="wallet:a",
        metadata={"raw_amount": "1000000"},
    )
    quote = PriceQuote("asset:usdc", "USDT", 1.0, now + timedelta(seconds=1), "fixture")
    result = await enrich_capital_event(
        event,
        registry=TokenRegistry([token]),
        price_book=PriceBook(quote),
        allow_usdt_usd_proxy=True,
    )
    assert result.usd_value is None
    assert result.metadata["valuation_status"] == "quote_future_or_stale"
