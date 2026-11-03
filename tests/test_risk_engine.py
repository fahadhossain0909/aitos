import pytest

from aitos.risk.models import (
    PortfolioState,
    PositionExposure,
    RiskAction,
    RiskLimits,
    RiskScoreBreakdown,
)
from aitos.risk.risk_engine import RiskEngine, _action_for_score, check_limits


def make_healthy_portfolio() -> PortfolioState:
    return PortfolioState(
        equity_usd=10_000.0,
        peak_equity_usd=10_000.0,
        positions=(
            PositionExposure(
                symbol="BTCUSDT",
                notional_usd=1000.0,
                leverage=3.0,
                sector="crypto-major",
            ),
        ),
        daily_pnl_pct=0.5,
        weekly_pnl_pct=1.0,
        volatility_percentile=20.0,
        regime="normal",
        max_pairwise_correlation=0.1,
        api_error_rate_pct=0.0,
        api_latency_ms=100.0,
        data_freshness_seconds=1.0,
        model_accuracy=0.8,
    )


def make_hard_breach_portfolio() -> PortfolioState:
    """Drawdown well past the hard cap (20%) — should force EMERGENCY_STOP
    regardless of the exact weighted score."""
    return PortfolioState(equity_usd=7000.0, peak_equity_usd=10_000.0)  # 30% drawdown


@pytest.mark.asyncio
async def test_healthy_portfolio_scores_normal_and_does_not_trip(event_bus):
    engine = RiskEngine(event_bus=event_bus)
    await engine.initialize({})

    breakdown = await engine.assess(make_healthy_portfolio())

    assert breakdown.action == RiskAction.NORMAL
    assert breakdown.total < 70
    assert engine.circuit_breaker.state.value == "closed"
    should_veto, _ = engine.veto()
    assert should_veto is False


@pytest.mark.asyncio
async def test_action_tier_boundaries():
    assert _action_for_score(50) == RiskAction.NORMAL
    assert _action_for_score(70) == RiskAction.NORMAL
    assert _action_for_score(70.1) == RiskAction.REDUCE_SIZE
    assert _action_for_score(85) == RiskAction.REDUCE_SIZE
    assert _action_for_score(85.1) == RiskAction.NO_NEW_ENTRIES
    assert _action_for_score(95) == RiskAction.NO_NEW_ENTRIES
    assert _action_for_score(95.1) == RiskAction.EMERGENCY_STOP


@pytest.mark.asyncio
async def test_hard_cap_breach_triggers_emergency_stop_and_opens_breaker(event_bus):
    engine = RiskEngine(event_bus=event_bus)
    await engine.initialize({})

    await engine.assess(make_hard_breach_portfolio())

    assert engine.circuit_breaker.state.value == "open"
    should_veto, reason = engine.veto()
    assert should_veto is True
    assert "circuit breaker" in reason


@pytest.mark.asyncio
async def test_veto_true_when_last_assessment_is_no_new_entries(event_bus):
    engine = RiskEngine(event_bus=event_bus)
    await engine.initialize({})

    # Directly install a NO_NEW_ENTRIES assessment to test the veto branch
    # that isn't circuit-breaker-driven, independent of exact score arithmetic.
    engine._last_assessment = RiskScoreBreakdown(
        position_risk=80,
        market_risk=90,
        system_risk=50,
        portfolio_risk=85,
        total=88.0,
        action=RiskAction.NO_NEW_ENTRIES,
    )
    should_veto, reason = engine.veto()
    assert should_veto is True
    assert "88.0" in reason


@pytest.mark.asyncio
async def test_assess_publishes_score_update_event(event_bus):
    received = []

    async def handler(event):
        received.append(event)

    await event_bus.subscribe("risk.score_update", handler, group="test")

    engine = RiskEngine(event_bus=event_bus)
    await engine.initialize({})
    await engine.assess(make_healthy_portfolio())

    import asyncio

    for _ in range(20):
        if received:
            break
        await asyncio.sleep(0.1)

    assert len(received) == 1
    assert received[0].topic == "risk.score_update"
    assert "total" in received[0].payload


@pytest.mark.asyncio
async def test_emergency_stop_publishes_critical_event(event_bus):
    from aitos.core.contracts import EventPriority

    received = []

    async def handler(event):
        received.append(event)

    await event_bus.subscribe("risk.emergency_stop", handler, group="test")

    engine = RiskEngine(event_bus=event_bus)
    await engine.initialize({})
    await engine.assess(make_hard_breach_portfolio())

    import asyncio

    for _ in range(20):
        if received:
            break
        await asyncio.sleep(0.1)

    assert len(received) == 1
    assert received[0].priority == EventPriority.CRITICAL


def test_check_limits_flags_drawdown_breach():
    limits = RiskLimits()
    portfolio = PortfolioState(
        equity_usd=8500.0, peak_equity_usd=10_000.0
    )  # 15% drawdown
    breaches = check_limits(portfolio, limits)
    names = [b.limit_name for b in breaches]
    assert "max_drawdown_pct" in names
    dd_breach = next(b for b in breaches if b.limit_name == "max_drawdown_pct")
    assert dd_breach.is_hard_cap is False  # 15% > 10% default, but < 20% hard cap


def test_check_limits_flags_hard_cap_when_exceeded():
    limits = RiskLimits()
    portfolio = PortfolioState(
        equity_usd=7000.0, peak_equity_usd=10_000.0
    )  # 30% drawdown
    breaches = check_limits(portfolio, limits)
    dd_breach = next(b for b in breaches if b.limit_name == "max_drawdown_pct")
    assert dd_breach.is_hard_cap is True


def test_check_limits_no_breaches_for_healthy_portfolio():
    limits = RiskLimits()
    breaches = check_limits(make_healthy_portfolio(), limits)
    assert breaches == []


@pytest.mark.asyncio
async def test_attempt_recovery_transitions_to_half_open_after_cooldown(event_bus):
    import asyncio

    engine = RiskEngine(event_bus=event_bus, circuit_breaker_cooldown_seconds=0.05)
    await engine.initialize({})
    await engine.trigger_emergency_stop("manual test trip")

    assert await engine.attempt_recovery() is False  # cooldown not elapsed yet
    await asyncio.sleep(0.06)
    assert await engine.attempt_recovery() is True
    assert engine.circuit_breaker.state.value == "half_open"
