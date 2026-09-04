from datetime import datetime, timedelta, timezone

import pytest

from aitos.intelligence.capital_gateway import CapitalGateway
from aitos.intelligence.capital_runtime import install_capital_guard
from aitos.models.trade import Opportunity, TradeLifecycleState, TradeSide
from aitos.risk.models import PortfolioState
from aitos.trading.lifecycle import TradeLifecycle


def make_portfolio(**overrides) -> PortfolioState:
    values = dict(equity_usd=10_000.0, peak_equity_usd=10_000.0)
    values.update(overrides)
    return PortfolioState(**values)


def make_opportunity(**overrides) -> Opportunity:
    values = dict(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100.0,
        stop_loss_price=99.0,
        take_profit_levels=[104.0],
        confidence=0.8,
        strategy_id="capital-test",
        rationale="capital objective test",
    )
    values.update(overrides)
    return Opportunity(**values)


def test_gateway_uses_nearest_target_and_execution_costs():
    estimate = CapitalGateway().estimate_opportunity(
        make_opportunity(take_profit_levels=[103.0, 110.0]),
        fee_bps=10.0,
        slippage_bps=5.0,
    )
    assert estimate.expected_gross_return_pct == pytest.approx(3.0)
    assert estimate.total_cost_pct > 0.15
    assert estimate.expected_net_edge_pct < 2.85


@pytest.mark.asyncio
async def test_runtime_guard_blocks_fee_heavy_or_low_edge_trade(event_bus, risk_engine):
    install_capital_guard()
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    opportunity = make_opportunity(take_profit_levels=[100.05], confidence=0.95)
    trade = await lifecycle.submit_opportunity(opportunity, make_portfolio())
    assert trade.state == TradeLifecycleState.REJECTED
    assert trade.rejection_reason.startswith("capital_objective:")
    assert lifecycle.get_open_trades() == []


@pytest.mark.asyncio
async def test_runtime_guard_rejects_stale_opportunity(event_bus, risk_engine):
    install_capital_guard()
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    stale = (datetime.now(timezone.utc) - timedelta(seconds=31)).isoformat()
    trade = await lifecycle.submit_opportunity(
        make_opportunity(detected_at=stale), make_portfolio()
    )
    assert trade.state == TradeLifecycleState.REJECTED
    assert "opportunity_expired" in trade.rejection_reason


@pytest.mark.asyncio
async def test_runtime_guard_halts_after_daily_loss(event_bus, risk_engine):
    install_capital_guard()
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(
        make_opportunity(), make_portfolio(daily_pnl_pct=-3.0)
    )
    assert trade.state == TradeLifecycleState.REJECTED
    assert "daily_loss_circuit_breaker" in trade.rejection_reason


@pytest.mark.asyncio
async def test_runtime_guard_allows_eligible_trade(event_bus, risk_engine):
    install_capital_guard()
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    assert trade.state == TradeLifecycleState.POSITION_OPENED
    assert trade.agent_consensus["capital_objective"]["eligible"] is True
    assert trade.agent_consensus["capital_objective"]["risk_budget_usd"] <= 100.0
