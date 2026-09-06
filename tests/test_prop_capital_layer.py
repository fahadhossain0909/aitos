from __future__ import annotations

import pytest

from aitos.capital.compliance import PropComplianceEngine
from aitos.capital.models import AccountMode, AccountSnapshot, PropFirmProfile, PropRuleSet
from aitos.capital.router import CapitalRouter
from aitos.execution.order_executor import OrderRequest, OrderResult, SimulatedOrderExecutor
from aitos.models.trade import TradeSide


@pytest.fixture
def request() -> OrderRequest:
    return OrderRequest(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=0.01,
        reference_price=50_000.0,
    )


def profile(**kwargs) -> PropFirmProfile:
    return PropFirmProfile(
        provider="test-prop",
        platform="api",
        account_mode=AccountMode.SIMULATED_FUNDED,
        live_api=False,
        rules=PropRuleSet(**kwargs),
    )


def account(**kwargs) -> AccountSnapshot:
    return AccountSnapshot(
        account_id="acct-1",
        provider="test-prop",
        equity=kwargs.get("equity", 100_000),
        balance=100_000,
        day_start_equity=kwargs.get("day_start_equity", 100_000),
        high_water_mark=kwargs.get("high_water_mark", 100_000),
    )


def test_daily_loss_is_fail_closed(request):
    engine = PropComplianceEngine()
    decision = engine.evaluate(
        profile(daily_loss_limit=5_000),
        account(equity=95_001),
        request,
        projected_loss=1.0,
    )
    assert not decision.allowed
    assert "daily loss" in decision.reason


def test_drawdown_is_enforced(request):
    engine = PropComplianceEngine()
    decision = engine.evaluate(
        profile(max_drawdown=10_000),
        account(equity=90_001),
        request,
        projected_loss=1.0,
    )
    assert not decision.allowed
    assert "drawdown" in decision.reason


def test_api_and_automation_policy_is_enforced(request):
    engine = PropComplianceEngine()
    assert not engine.evaluate(profile(api_allowed=False), account(), request).allowed
    assert not engine.evaluate(profile(automation_allowed=False), account(), request).allowed


@pytest.mark.asyncio
async def test_router_selects_compliant_venue(request):
    router = CapitalRouter()
    safe = profile(daily_loss_limit=5_000)
    unsafe = profile(daily_loss_limit=1)
    router.register("unsafe", SimulatedOrderExecutor(), unsafe, account(equity=99_999))
    router.register("safe", SimulatedOrderExecutor(), safe, account())

    venue, result = await router.submit_best(request)
    assert venue == "safe"
    assert result.success


def test_simulated_executor_remains_compatible(request):
    result = __import__("asyncio").run(SimulatedOrderExecutor().submit_order(request))
    assert isinstance(result, OrderResult)
    assert result.success
