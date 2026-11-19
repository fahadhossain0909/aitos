from decimal import Decimal
from types import SimpleNamespace

import pytest

from aitos.spot.commission import SpotCommissionConversionService
from aitos.spot.inventory import InventoryLedger


class HistoricalFeeAdapter:
    async def exchange_info(self):
        return {
            "BNBUSDT": SimpleNamespace(status="TRADING", is_spot_trading_allowed=True)
        }

    async def agg_trades(self, symbol, *, start_time_ms, end_time_ms, limit):
        assert symbol == "BNBUSDT"
        return [
            {"T": end_time_ms - 1000, "p": "300"},
            {"T": end_time_ms - 100, "p": "310"},
            {"T": end_time_ms + 1, "p": "999"},
        ]


@pytest.mark.asyncio
async def test_third_asset_fee_uses_latest_historical_trade_before_fill():
    ledger = InventoryLedger()
    ledger.record_fill(
        fill_key="fee-fill",
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        side="BUY",
        quantity=Decimal(1),
        price=Decimal(100),
        commission=Decimal("0.01"),
        commission_asset="BNB",
        fill_time_ms=1_700_000_000_000,
    )
    result = await SpotCommissionConversionService(
        adapter=HistoricalFeeAdapter(), inventory=ledger
    ).resolve_pending()
    assert result == {"resolved": 1, "pending": 0}
    position = ledger.get_position("BTCUSDT")
    assert position.cost_quote == Decimal("103.10")
    assert not position.fee_conversion_pending
    provenance = ledger.commission_conversion_provenance("fee-fill")
    assert provenance["symbol"] == "BNBUSDT"
    assert provenance["price"] == "310"
    assert provenance["time_ms"] == 1_700_000_000_000 - 100
    assert provenance["source"] == "binance_spot_aggTrades"
    ledger.close()


@pytest.mark.asyncio
async def test_third_asset_fee_stays_pending_without_fill_time():
    ledger = InventoryLedger()
    ledger.record_fill(
        fill_key="no-time",
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        side="BUY",
        quantity=Decimal(1),
        price=Decimal(100),
        commission=Decimal("0.01"),
        commission_asset="BNB",
    )
    result = await SpotCommissionConversionService(
        adapter=HistoricalFeeAdapter(), inventory=ledger
    ).resolve_pending()
    assert result == {"resolved": 0, "pending": 1}
    assert ledger.get_position("BTCUSDT").fee_conversion_pending
    ledger.close()
