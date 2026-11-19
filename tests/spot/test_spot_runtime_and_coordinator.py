from decimal import Decimal
from types import SimpleNamespace

import pytest

from aitos.config.settings import SpotSettings
from aitos.models.trade import Opportunity, TradeSide
from aitos.spot.coordinator import SpotAuthorizationRejected, SpotTradingCoordinator
from aitos.spot.inventory import InventoryLedger
from aitos.spot.models import SpotOrderRequest, SpotOrderSide, SpotOrderType


def test_spot_settings_are_disabled_and_testnet_only_by_default():
    settings = SpotSettings(_env_file=None)
    assert settings.enabled is False
    assert settings.testnet is True
    assert settings.execution_enabled is False
    assert settings.allow_mainnet is False


def test_spot_settings_reject_execution_without_opt_in():
    with pytest.raises(ValueError, match="requires AITOS_SPOT_ENABLED"):
        SpotSettings(_env_file=None, execution_enabled=True)


def test_spot_settings_reject_conflicting_mainnet_and_testnet_flags():
    with pytest.raises(ValueError, match="cannot be combined"):
        SpotSettings(_env_file=None, testnet=True, allow_mainnet=True)


class FakeCapitalGateway:
    def __init__(self):
        self.symbol = None
        self.kwargs = None
        self.pinned = []
        self.released = []

    def authorize_opportunity(self, equity, opportunity, **kwargs):
        self.symbol = opportunity.symbol
        self.kwargs = kwargs
        return (SimpleNamespace(eligible=True, rejection_reasons=()),
                SimpleNamespace(capital_usd=Decimal("100")))

    def pin(self, symbol):
        self.pinned.append(symbol)
        return True

    def release(self, symbol):
        self.released.append(symbol)
        return object()


class FakeExecutor:
    async def submit(self, request):
        return request


def _opportunity(side=TradeSide.LONG):
    return Opportunity(
        symbol="BTCUSDT", side=side, entry_price=100,
        stop_loss_price=98, take_profit_levels=[104], confidence=0.9,
        strategy_id="spot-test", rationale="unit test",
    )


@pytest.mark.asyncio
async def test_spot_entry_uses_shared_capital_gate_and_namespaced_reservation():
    gateway = FakeCapitalGateway()
    coordinator = SpotTradingCoordinator(
        capital_gateway=gateway, executor=FakeExecutor(),
        inventory=InventoryLedger(),
    )
    request = SpotOrderRequest(
        "BTCUSDT", SpotOrderSide.BUY, SpotOrderType.MARKET,
        quote_order_quantity=Decimal("50"),
    )
    result = await coordinator.submit_entry(
        equity_quote=1000, opportunity=_opportunity(), request=request,
    )
    assert result is request
    assert gateway.symbol == "binance:spot:BTCUSDT"
    assert gateway.kwargs["funding_bps"] == 0.0
    assert gateway.pinned == ["binance:spot:BTCUSDT"]


@pytest.mark.asyncio
async def test_spot_entry_rejects_short_and_oversized_requests():
    gateway = FakeCapitalGateway()
    coordinator = SpotTradingCoordinator(
        capital_gateway=gateway, executor=FakeExecutor(),
        inventory=InventoryLedger(),
    )
    request = SpotOrderRequest(
        "BTCUSDT", SpotOrderSide.BUY, SpotOrderType.MARKET,
        quote_order_quantity=Decimal("101"),
    )
    with pytest.raises(SpotAuthorizationRejected, match="long-only"):
        await coordinator.submit_entry(
            equity_quote=1000, opportunity=_opportunity(TradeSide.SHORT),
            request=request,
        )
    with pytest.raises(SpotAuthorizationRejected, match="exceeds approved"):
        await coordinator.submit_entry(
            equity_quote=1000, opportunity=_opportunity(), request=request,
        )


@pytest.mark.asyncio
async def test_spot_exit_requires_authorization_and_managed_inventory():
    ledger = InventoryLedger()
    ledger.record_fill(
        fill_key="buy-1", symbol="BTCUSDT", base_asset="BTC",
        quote_asset="USDT", side="BUY", quantity=Decimal("1"),
        price=Decimal("100"),
    )
    coordinator = SpotTradingCoordinator(
        capital_gateway=FakeCapitalGateway(), executor=FakeExecutor(),
        inventory=ledger,
    )
    with pytest.raises(SpotAuthorizationRejected, match="Exit Intelligence"):
        await coordinator.submit_exit(
            symbol="BTCUSDT", quantity=Decimal("0.5"), exit_authorized=False,
        )
    with pytest.raises(SpotAuthorizationRejected, match="managed Spot inventory"):
        await coordinator.submit_exit(
            symbol="BTCUSDT", quantity=Decimal("2"), exit_authorized=True,
        )
    result = await coordinator.submit_exit(
        symbol="BTCUSDT", quantity=Decimal("0.5"), exit_authorized=True,
    )
    assert result.side is SpotOrderSide.SELL
