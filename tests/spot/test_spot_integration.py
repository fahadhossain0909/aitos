from decimal import Decimal

import pytest

from aitos.spot.filters import parse_spot_exchange_info
from aitos.spot.inventory import InventoryLedger
from aitos.spot.models import SpotOrderRequest, SpotOrderSide, SpotOrderType
from aitos.spot.scanner import SpotCandidate, SpotOpportunityScanner
from aitos.spot.binance import BinanceSpotAdapter
from aitos.spot.execution import SpotExecutionDisabled, SpotOrderExecutor


def _exchange_info():
    return {"symbols": [{
        "symbol": "ABCUSDT", "status": "TRADING", "baseAsset": "ABC",
        "quoteAsset": "USDT", "isSpotTradingAllowed": True,
        "quoteOrderQtyMarketAllowed": True,
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01", "maxPrice": "1000"},
            {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1", "maxQty": "10000"},
            {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.1", "minQty": "0.1", "maxQty": "10000"},
            {"filterType": "NOTIONAL", "minNotional": "5", "maxNotional": "100000",
             "applyMinToMarket": True, "applyMaxToMarket": False},
        ],
    }]}


def test_spot_filters_decimal_rounding_and_notional():
    f = parse_spot_exchange_info(_exchange_info())["ABCUSDT"]
    assert f.round_quantity(Decimal("1.29")) == Decimal("1.2")
    assert f.round_price(Decimal("12.349")) == Decimal("12.34")
    assert f.validate(Decimal("1.2"), Decimal("10")) == Decimal("1.2")
    with pytest.raises(ValueError):
        f.validate(Decimal("0.1"), Decimal("10"))


def test_spot_order_request_requires_exactly_one_quantity_mode():
    with pytest.raises(ValueError):
        SpotOrderRequest("ABCUSDT", SpotOrderSide.BUY, SpotOrderType.MARKET)
    with pytest.raises(ValueError):
        SpotOrderRequest("ABCUSDT", SpotOrderSide.BUY, SpotOrderType.MARKET,
                         quantity=Decimal("1"), quote_order_quantity=Decimal("10"))


def test_inventory_fill_idempotency_and_fee_accounting():
    ledger = InventoryLedger()
    assert ledger.record_fill(fill_key="buy-1", symbol="ABCUSDT", base_asset="ABC",
        quote_asset="USDT", side="BUY", quantity=Decimal("2"), price=Decimal("10"),
        commission=Decimal("0.01"), commission_asset="ABC")
    assert not ledger.record_fill(fill_key="buy-1", symbol="ABCUSDT", base_asset="ABC",
        quote_asset="USDT", side="BUY", quantity=Decimal("2"), price=Decimal("10"),
        commission=Decimal("0.01"), commission_asset="ABC")
    pos = ledger.get_position("ABCUSDT")
    assert pos.quantity == Decimal("1.99")
    assert pos.cost_quote == Decimal("20")
    ledger.record_fill(fill_key="sell-1", symbol="ABCUSDT", base_asset="ABC",
        quote_asset="USDT", side="SELL", quantity=Decimal("1"), price=Decimal("12"),
        commission=Decimal("0.12"), commission_asset="USDT")
    pos = ledger.get_position("ABCUSDT")
    assert pos.quantity == Decimal("0.99")
    assert pos.realized_quote_pnl == Decimal("11.88") - (Decimal("20") / Decimal("1.99"))


def test_inventory_rejects_unowned_sell():
    ledger = InventoryLedger()
    with pytest.raises(ValueError):
        ledger.record_fill(fill_key="sell", symbol="ABCUSDT", base_asset="ABC",
            quote_asset="USDT", side="SELL", quantity=Decimal("1"), price=Decimal("10"))


def test_scanner_filters_stale_and_illiquid_candidates():
    scanner = SpotOpportunityScanner(min_quote_volume=Decimal("1000"))
    items = [
        SpotCandidate("GOODUSDT", Decimal("5000"), Decimal("2"), Decimal("3"),
                      Decimal("200"), Decimal("80"), Decimal("0.4")),
        SpotCandidate("STALEUSDT", Decimal("5000"), Decimal("2"), Decimal("3"),
                      Decimal("200"), Decimal("80"), Decimal("0.4"), data_fresh=False),
        SpotCandidate("WIDEUSDT", Decimal("5000"), Decimal("2"), Decimal("50"),
                      Decimal("200"), Decimal("80"), Decimal("0.4")),
    ]
    ranked = scanner.rank(items)
    assert [x.candidate.symbol for x in ranked] == ["GOODUSDT"]


@pytest.mark.asyncio
async def test_execution_disabled_by_default():
    adapter = BinanceSpotAdapter(testnet=True)
    executor = SpotOrderExecutor(adapter)
    request = SpotOrderRequest("ABCUSDT", SpotOrderSide.BUY, SpotOrderType.MARKET,
                               quote_order_quantity=Decimal("10"))
    with pytest.raises(SpotExecutionDisabled):
        await executor.submit(request)
    await adapter.close()

def test_inventory_persists_across_reopen(tmp_path):
    path = tmp_path / "spot-ledger.sqlite"
    ledger = InventoryLedger(path)
    ledger.record_fill(fill_key="persistent-buy", symbol="ABCUSDT", base_asset="ABC",
        quote_asset="USDT", side="BUY", quantity=Decimal("1"), price=Decimal("5"))
    ledger.close()
    reopened = InventoryLedger(path)
    assert reopened.get_position("ABCUSDT").quantity == Decimal("1")
    reopened.close()


@pytest.mark.asyncio
async def test_mainnet_execution_requires_second_explicit_guard():
    adapter = BinanceSpotAdapter(testnet=False)
    executor = SpotOrderExecutor(adapter, enabled=True, allow_mainnet=False)
    request = SpotOrderRequest("ABCUSDT", SpotOrderSide.BUY, SpotOrderType.MARKET,
                               quote_order_quantity=Decimal("10"))
    with pytest.raises(SpotExecutionDisabled):
        await executor.submit(request)
    await adapter.close()
