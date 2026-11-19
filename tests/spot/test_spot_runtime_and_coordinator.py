from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from aitos.config.settings import SpotSettings
from aitos.intelligence.exit_intelligence.models import ExitAction, ExitDecision
from aitos.models.trade import Opportunity, TradeSide
from aitos.spot.coordinator import SpotAuthorizationRejected, SpotTradingCoordinator
from aitos.spot.execution import SpotExecutionDisabled
from aitos.spot.inventory import InventoryLedger
from aitos.spot.runtime import SpotRuntime
from aitos.spot.portfolio import SpotPortfolioValuator
from aitos.spot.scanner import (
    RankedSpotCandidate,
    SpotCandidate,
    SpotOpportunityScanner,
)
from aitos.spot.models import (
    SpotOrderRequest,
    SpotOrderSide,
    SpotOrderStatus,
    SpotOrderType,
)


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
        self.opportunity = None
        self.pinned = []
        self.released = []

    def authorize_opportunity(self, equity, opportunity, **kwargs):
        self.symbol = opportunity.symbol
        self.opportunity = opportunity
        self.kwargs = kwargs
        return (
            SimpleNamespace(eligible=True, rejection_reasons=()),
            SimpleNamespace(capital_usd=Decimal("100")),
        )

    def pin(self, symbol):
        self.pinned.append(symbol)
        return True

    def release(self, symbol):
        self.released.append(symbol)
        return object()


class FakeExecutor:
    enabled = True

    async def submit(self, request):
        return SimpleNamespace(
            client_order_id=request.client_order_id,
            side=request.side,
            status=SpotOrderStatus.NEW,
            order_id=123,
        )


def _exit_decision(action=ExitAction.EXIT, fraction=1.0):
    return ExitDecision(
        symbol="BTCUSDT",
        side="LONG",
        action=action,
        exit_score=0.9,
        expected_remaining_edge=-0.1,
        reasons=(),
        suggested_reduce_fraction=fraction,
        as_of=datetime.now(timezone.utc),
    )


def _opportunity(side=TradeSide.LONG):
    return Opportunity(
        symbol="BTCUSDT",
        side=side,
        entry_price=100,
        stop_loss_price=98,
        take_profit_levels=[104],
        confidence=0.9,
        strategy_id="spot-test",
        rationale="unit test",
    )


@pytest.mark.asyncio
async def test_spot_entry_uses_shared_capital_gate_and_namespaced_reservation():
    gateway = FakeCapitalGateway()
    coordinator = SpotTradingCoordinator(
        capital_gateway=gateway,
        executor=FakeExecutor(),
        inventory=InventoryLedger(),
    )
    request = SpotOrderRequest(
        "BTCUSDT",
        SpotOrderSide.BUY,
        SpotOrderType.MARKET,
        quote_order_quantity=Decimal("50"),
    )
    opportunity = replace(
        _opportunity(),
        agent_consensus={"leverage": 20.0, "funding_bps": 30.0},
    )
    result = await coordinator.submit_entry(
        equity_usd=1000,
        opportunity=opportunity,
        request=request,
    )
    assert result.client_order_id.startswith("aitos-")
    assert gateway.symbol == "binance:spot:BTCUSDT"
    assert gateway.kwargs["funding_bps"] == 0.0
    assert gateway.opportunity.agent_consensus["leverage"] == 1.0
    assert gateway.opportunity.agent_consensus["funding_bps"] == 0.0
    assert gateway.pinned == ["binance:spot:BTCUSDT"]


@pytest.mark.asyncio
async def test_spot_entry_rejects_short_and_oversized_requests():
    gateway = FakeCapitalGateway()
    coordinator = SpotTradingCoordinator(
        capital_gateway=gateway,
        executor=FakeExecutor(),
        inventory=InventoryLedger(),
    )
    request = SpotOrderRequest(
        "BTCUSDT",
        SpotOrderSide.BUY,
        SpotOrderType.MARKET,
        quote_order_quantity=Decimal("101"),
    )
    with pytest.raises(SpotAuthorizationRejected, match="long-only"):
        await coordinator.submit_entry(
            equity_usd=1000,
            opportunity=_opportunity(TradeSide.SHORT),
            request=request,
        )
    with pytest.raises(SpotAuthorizationRejected, match="exceeds approved"):
        await coordinator.submit_entry(
            equity_usd=1000,
            opportunity=_opportunity(),
            request=request,
        )


@pytest.mark.asyncio
async def test_spot_exit_requires_authorization_and_managed_inventory():
    ledger = InventoryLedger()
    ledger.record_fill(
        fill_key="buy-1",
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        side="BUY",
        quantity=Decimal("1"),
        price=Decimal("100"),
    )
    coordinator = SpotTradingCoordinator(
        capital_gateway=FakeCapitalGateway(),
        executor=FakeExecutor(),
        inventory=ledger,
    )
    with pytest.raises(SpotAuthorizationRejected, match="does not authorize"):
        await coordinator.submit_exit(
            symbol="BTCUSDT",
            quantity=Decimal("0.5"),
            authorization=_exit_decision(ExitAction.HOLD),
        )
    with pytest.raises(SpotAuthorizationRejected, match="managed Spot inventory"):
        await coordinator.submit_exit(
            symbol="BTCUSDT",
            quantity=Decimal("2"),
            authorization=_exit_decision(),
        )
    with pytest.raises(SpotAuthorizationRejected, match="EIE suggested"):
        await coordinator.submit_exit(
            symbol="BTCUSDT",
            quantity=Decimal("0.5"),
            authorization=_exit_decision(ExitAction.MANAGE, fraction=0.25),
        )
    result = await coordinator.submit_exit(
        symbol="BTCUSDT",
        quantity=Decimal("0.5"),
        authorization=_exit_decision(),
    )
    assert result.side is SpotOrderSide.SELL


def test_spot_settings_reject_non_usdt_capital_numeraire():
    with pytest.raises(ValueError, match="requires USDT"):
        SpotSettings(_env_file=None, quote_asset="BTC")


@pytest.mark.asyncio
async def test_disabled_spot_execution_does_not_reserve_or_journal():
    gateway = FakeCapitalGateway()
    executor = FakeExecutor()
    executor.enabled = False
    ledger = InventoryLedger()
    coordinator = SpotTradingCoordinator(
        capital_gateway=gateway, executor=executor, inventory=ledger
    )
    request = SpotOrderRequest(
        "BTCUSDT",
        SpotOrderSide.BUY,
        SpotOrderType.MARKET,
        quote_order_quantity=Decimal("10"),
    )
    with pytest.raises(SpotExecutionDisabled, match="Spot execution is disabled"):
        await coordinator.submit_entry(
            equity_usd=1000, opportunity=_opportunity(), request=request
        )
    assert gateway.pinned == []
    assert ledger.unresolved_order_intents() == []


class FakeSharedStrategyScanner:
    async def decide_with_kernel(self, candidate, kernel):
        return SimpleNamespace(direction="long", confidence=0.8)

    def to_opportunity(self, candidate, *, is_production, approved_by):
        assert is_production is False
        return _opportunity()


@pytest.mark.asyncio
async def test_spot_runtime_routes_shared_kernel_approved_candidate():
    gateway = FakeCapitalGateway()
    inventory = InventoryLedger()
    coordinator = SpotTradingCoordinator(
        capital_gateway=gateway,
        executor=FakeExecutor(),
        inventory=inventory,
    )
    runtime = SpotRuntime(
        market_data=SimpleNamespace(),
        executor=FakeExecutor(),
        inventory=inventory,
        scanner=SimpleNamespace(),
        coordinator=coordinator,
        market_consumer=SimpleNamespace(),
        lead_lag=SimpleNamespace(),
        quote_volumes_24h={"BTCUSDT": Decimal("100000")},
    )
    candidate = SimpleNamespace(symbol="BTCUSDT", direction=TradeSide.LONG)
    kernel = SimpleNamespace(fusion_min_confidence=0.6)

    result = await runtime.submit_strategy_candidate(
        candidate=candidate,
        strategy_scanner=FakeSharedStrategyScanner(),
        kernel=kernel,
        equity_usd=1000,
        quote_order_quantity=Decimal("50"),
    )

    assert result.status is SpotOrderStatus.NEW
    assert gateway.symbol == "binance:spot:BTCUSDT"
    assert gateway.opportunity.agent_consensus["market_type"] == "spot"
    inventory.close()


@pytest.mark.asyncio
async def test_spot_runtime_rejects_candidates_outside_discovered_universe():
    inventory = InventoryLedger()
    runtime = SpotRuntime(
        market_data=SimpleNamespace(),
        executor=FakeExecutor(),
        inventory=inventory,
        scanner=SimpleNamespace(),
        coordinator=SimpleNamespace(),
        market_consumer=SimpleNamespace(),
        lead_lag=SimpleNamespace(),
        quote_volumes_24h={"ETHUSDT": Decimal("100000")},
    )
    candidate = SimpleNamespace(symbol="BTCUSDT", direction=TradeSide.LONG)
    with pytest.raises(ValueError, match="outside the active Spot universe"):
        await runtime.submit_strategy_candidate(
            candidate=candidate,
            strategy_scanner=FakeSharedStrategyScanner(),
            kernel=SimpleNamespace(fusion_min_confidence=0.6),
            equity_usd=1000,
            quote_order_quantity=Decimal("50"),
        )
    inventory.close()


@pytest.mark.asyncio
async def test_spot_capital_allocation_converts_usd_to_quote_notional():
    gateway = FakeCapitalGateway()
    ledger = InventoryLedger()
    coordinator = SpotTradingCoordinator(
        capital_gateway=gateway,
        executor=FakeExecutor(),
        inventory=ledger,
        quote_to_usd_rate=Decimal("2"),
    )
    request = SpotOrderRequest(
        "BTCUSDT",
        SpotOrderSide.BUY,
        SpotOrderType.MARKET,
        quote_order_quantity=Decimal("51"),
    )
    with pytest.raises(SpotAuthorizationRejected, match="exceeds approved"):
        await coordinator.submit_entry(
            equity_usd=1000,
            opportunity=_opportunity(),
            request=request,
        )
    assert gateway.released == ["binance:spot:BTCUSDT"]
    ledger.close()


class FakeKernelDecision:
    fusion_min_confidence = 0.6

    async def request_decision(self, context):
        assert context.symbol == "BTCUSDT"
        assert context.context["market_type"] == "spot"
        assert (
            context.context["futures_spot_lead_lag_shadow"]["decision_impact"] is False
        )
        return SimpleNamespace(direction="long", confidence=0.85)


@pytest.mark.asyncio
async def test_spot_prefilter_candidate_uses_kernel_and_explicit_strategy_builder():
    gateway = FakeCapitalGateway()
    inventory = InventoryLedger()
    coordinator = SpotTradingCoordinator(
        capital_gateway=gateway,
        executor=FakeExecutor(),
        inventory=inventory,
    )
    runtime = SpotRuntime(
        market_data=SimpleNamespace(),
        executor=FakeExecutor(),
        inventory=inventory,
        scanner=SpotOpportunityScanner(),
        coordinator=coordinator,
        market_consumer=SimpleNamespace(),
        lead_lag=SimpleNamespace(),
        quote_volumes_24h={"BTCUSDT": Decimal("100000")},
    )
    candidate = SpotCandidate(
        symbol="BTCUSDT",
        quote_volume=Decimal("100000"),
        relative_volume=Decimal("2"),
        spread_bps=Decimal("2"),
        volatility_bps=Decimal("100"),
        momentum_bps=Decimal("50"),
        order_book_imbalance=Decimal("0.2"),
        last_price=Decimal("100"),
    )
    ranked = RankedSpotCandidate(candidate, Decimal("0.8"), ("fresh",))
    result = await runtime.submit_ranked_spot_candidate(
        ranked_candidate=ranked,
        kernel=FakeKernelDecision(),
        opportunity_builder=lambda item, decision: _opportunity(),
        equity_usd=1000,
        quote_order_quantity=Decimal("50"),
    )
    assert result.status is SpotOrderStatus.NEW
    assert gateway.opportunity.agent_consensus["market_type"] == "spot"
    inventory.close()


@pytest.mark.asyncio
async def test_spot_candidate_kernel_short_is_never_authorized():
    class ShortKernel(FakeKernelDecision):
        async def request_decision(self, context):
            return SimpleNamespace(direction="short", confidence=0.99)

    inventory = InventoryLedger()
    runtime = SpotRuntime(
        market_data=SimpleNamespace(),
        executor=FakeExecutor(),
        inventory=inventory,
        scanner=SpotOpportunityScanner(),
        coordinator=SimpleNamespace(),
        market_consumer=SimpleNamespace(),
        lead_lag=SimpleNamespace(),
        quote_volumes_24h={"BTCUSDT": Decimal("100000")},
    )
    candidate = SpotCandidate(
        symbol="BTCUSDT",
        quote_volume=Decimal("100000"),
        relative_volume=Decimal("2"),
        spread_bps=Decimal("2"),
        volatility_bps=Decimal("100"),
        momentum_bps=Decimal("50"),
        order_book_imbalance=Decimal("0.2"),
        last_price=Decimal("100"),
    )
    with pytest.raises(ValueError, match="did not approve"):
        await runtime.submit_ranked_spot_candidate(
            ranked_candidate=RankedSpotCandidate(candidate, Decimal("0.8"), ()),
            kernel=ShortKernel(),
            opportunity_builder=lambda item, decision: _opportunity(),
            equity_usd=1000,
            quote_order_quantity=Decimal("50"),
        )
    inventory.close()



def test_spot_runtime_updates_shared_capital_and_portfolio_fx_rate():
    inventory = InventoryLedger()
    coordinator = SpotTradingCoordinator(
        capital_gateway=FakeCapitalGateway(),
        executor=FakeExecutor(),
        inventory=inventory,
    )
    runtime = SpotRuntime(
        market_data=SimpleNamespace(),
        executor=FakeExecutor(),
        inventory=inventory,
        scanner=SpotOpportunityScanner(),
        coordinator=coordinator,
        market_consumer=SimpleNamespace(),
        lead_lag=SimpleNamespace(),
        portfolio_valuator=SpotPortfolioValuator(inventory),
    )
    runtime.update_quote_to_usd_rate(Decimal("0.999"))
    assert coordinator.quote_to_usd_rate == Decimal("0.999")
    assert runtime.portfolio_valuator.quote_to_usd_rate == Decimal("0.999")
    with pytest.raises(ValueError, match="positive and finite"):
        runtime.update_quote_to_usd_rate(Decimal("0"))
    inventory.close()
