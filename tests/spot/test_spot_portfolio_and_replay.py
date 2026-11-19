from decimal import Decimal

from aitos.spot.binance import BinanceSpotAdapter
from aitos.spot.inventory import InventoryLedger
from aitos.spot.models import SpotBalance
from aitos.spot.portfolio import SpotPortfolioValuator
from aitos.spot.replay import BookLevel, replay_spot_market_order


def test_spot_portfolio_marks_managed_inventory_and_separate_account_cash():
    ledger = InventoryLedger()
    ledger.record_fill(
        fill_key="btc-buy",
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        side="BUY",
        quantity=Decimal("0.1"),
        price=Decimal("100"),
    )
    adapter = BinanceSpotAdapter(api_key="x", api_secret="y", testnet=True)
    valuator = SpotPortfolioValuator(ledger)
    import asyncio

    snapshot = asyncio.run(
        valuator.snapshot(
            spot_adapter=adapter,
            futures_account_equity_quote=Decimal("1000"),
            balances=[
                SpotBalance("USDT", Decimal("50"), Decimal("10")),
                SpotBalance("BTC", Decimal("9"), Decimal("0")),
            ],
            marks={"BTCUSDT": Decimal("120")},
        )
    )
    assert snapshot.spot_quote_cash == Decimal("60")
    assert snapshot.managed_inventory_market_value == Decimal("12.0")
    assert snapshot.total_equity_quote == Decimal("1072.0")
    assert snapshot.accounting_complete
    ledger.close()


def test_spot_portfolio_marks_missing_and_external_base_holdings_not_adopted():
    ledger = InventoryLedger()
    ledger.record_fill(
        fill_key="eth-buy",
        symbol="ETHUSDT",
        base_asset="ETH",
        quote_asset="USDT",
        side="BUY",
        quantity=Decimal("1"),
        price=Decimal("10"),
    )
    adapter = BinanceSpotAdapter(api_key="x", api_secret="y", testnet=True)
    import asyncio

    snapshot = asyncio.run(
        SpotPortfolioValuator(ledger).snapshot(
            spot_adapter=adapter,
            futures_account_equity_quote=Decimal("100"),
            balances=[SpotBalance("USDT", Decimal("20"), Decimal("0")),
                      SpotBalance("ETH", Decimal("50"), Decimal("0"))],
            marks={},
        )
    )
    assert snapshot.managed_inventory_market_value == 0
    assert snapshot.marks_missing == ("ETHUSDT",)
    assert not snapshot.accounting_complete
    ledger.close()


def test_spot_execution_replay_walks_depth_and_preserves_partial_fill():
    result = replay_spot_market_order(
        side="BUY",
        quantity=Decimal("3"),
        bids=[BookLevel(Decimal("99"), Decimal("10"))],
        asks=[
            BookLevel(Decimal("100"), Decimal("1")),
            BookLevel(Decimal("101"), Decimal("1")),
        ],
        maker_fee_bps=Decimal("10"),
        taker_fee_bps=Decimal("20"),
        latency_ms=125,
        impact_bps_per_1pct_depth=Decimal("0.1"),
        reference_mid=Decimal("99.5"),
    )
    assert result.filled_quantity == Decimal("2")
    assert result.unfilled_quantity == Decimal("1")
    assert result.fees_quote == Decimal("0.402")
    assert result.latency_ms == 125
    assert result.market_impact_bps > 0
    assert not result.complete
