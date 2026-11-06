from datetime import datetime, timedelta, timezone

import pytest

import aitos.intelligence.capital_runtime as capital_runtime
from aitos.intelligence.capital_controls import CapitalControlConfig
from aitos.intelligence.capital_gateway import CapitalGateway
from aitos.intelligence.capital_runtime import (
    _portfolio_consensus,
    install_capital_guard,
)
from aitos.models.trade import Opportunity, TradeLifecycleState, TradeSide
from aitos.risk.models import PortfolioState, PositionExposure
from aitos.trading.lifecycle import TradeLifecycle


@pytest.fixture(autouse=True)
def isolate_capital_runtime_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(
        capital_runtime,
        "get_model_data_dir",
        lambda: tmp_path,
    )


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
async def test_two_consecutive_losses_halves_next_risk_budget(event_bus, risk_engine):
    install_capital_guard()
    normal_lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    reduced_lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await normal_lifecycle.initialize({})
    await reduced_lifecycle.initialize({})
    normal = await normal_lifecycle.submit_opportunity(
        make_opportunity(), make_portfolio(consecutive_losses=0)
    )
    assert normal.state == TradeLifecycleState.POSITION_OPENED
    normal_risk = normal.agent_consensus["capital_objective"]["risk_budget_usd"]
    await normal_lifecycle.close_trade(
        normal.trade_id, normal.entry_price, "release_test_reservation"
    )
    reduced = await reduced_lifecycle.submit_opportunity(
        make_opportunity(), make_portfolio(consecutive_losses=2)
    )
    assert reduced.state == TradeLifecycleState.POSITION_OPENED
    reduced_risk = reduced.agent_consensus["capital_objective"]["risk_budget_usd"]
    assert reduced_risk == pytest.approx(normal_risk * 0.5)
    # The approved budget must cap the real sized position, not just metadata.
    assert reduced.risk_amount_usd <= reduced_risk + 1e-8
    assert reduced.risk_amount_usd == pytest.approx(normal.risk_amount_usd * 0.5)
    await reduced_lifecycle.close_trade(
        reduced.trade_id, reduced.entry_price, "release_test_reservation"
    )
    await normal_lifecycle.shutdown()
    await reduced_lifecycle.shutdown()


@pytest.mark.asyncio
async def test_runtime_guard_allows_eligible_trade(event_bus, risk_engine):
    install_capital_guard()
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    assert trade.state == TradeLifecycleState.POSITION_OPENED
    assert trade.agent_consensus["capital_objective"]["eligible"] is True
    assert trade.agent_consensus["capital_objective"]["risk_budget_usd"] <= 100.0
    funnel = lifecycle._aitos_directional_funnel.snapshot()
    assert funnel["stages"]["risk_gate"]["by_side"]["LONG"]["accepted"] >= 1
    assert funnel["stages"]["capital_gate"]["by_side"]["LONG"]["accepted"] >= 1
    assert funnel["stages"]["lifecycle_open"]["by_side"]["LONG"]["accepted"] >= 1
    await lifecycle.shutdown()


def test_gateway_decays_expected_edge_and_confidence_with_opportunity_age(monkeypatch):
    import aitos.intelligence.capital_gateway as gateway_module

    estimate = CapitalGateway().estimate_opportunity(make_opportunity())
    fresh_gateway = CapitalGateway(
        controls=CapitalControlConfig(opportunity_decay_half_life_seconds=15.0)
    )
    stale_gateway = CapitalGateway(
        controls=CapitalControlConfig(opportunity_decay_half_life_seconds=15.0)
    )

    monkeypatch.setattr(gateway_module, "opportunity_age_seconds", lambda _: 0.0)
    fresh = fresh_gateway.evaluate(10_000.0, [estimate]).decisions[0]
    monkeypatch.setattr(gateway_module, "opportunity_age_seconds", lambda _: 15.0)
    stale = stale_gateway.evaluate(10_000.0, [estimate]).decisions[0]

    assert stale.expected_net_edge_pct < fresh.expected_net_edge_pct
    assert any("opportunity_decay_factor=0.500000" in item for item in stale.rationale)
    assert any("opportunity_age_seconds=15.000" in item for item in stale.rationale)


def test_opportunity_decay_half_life_must_be_positive():
    with pytest.raises(ValueError, match="opportunity_decay_half_life_seconds"):
        CapitalControlConfig(opportunity_decay_half_life_seconds=0.0)


def test_gateway_preserves_negative_funding_cost_as_credit():
    estimate = CapitalGateway().estimate_opportunity(
        make_opportunity(),
        fee_bps=4.0,
        slippage_bps=2.0,
        funding_bps=-8.0,
    )
    assert estimate.funding_bps == -8.0
    assert estimate.total_cost_pct < 0.0


def test_portfolio_consensus_uses_actual_stop_risk_and_position_side():
    portfolio = make_portfolio(
        positions=(
            PositionExposure(
                symbol="BTCUSDT",
                notional_usd=5_000.0,
                leverage=5.0,
                side="SHORT",
                risk_amount_usd=150.0,
            ),
        ),
        max_pairwise_correlation=0.85,
    )
    opportunity = make_opportunity(symbol="ETHUSDT")
    result = _portfolio_consensus(portfolio, opportunity)
    assert result["position_risk_pct"]["BTCUSDT"] == pytest.approx(1.5)
    assert result["position_sides"]["BTCUSDT"] == "SHORT"
    assert result["correlations"]["BTCUSDT:ETHUSDT"] == pytest.approx(0.85)


def test_portfolio_consensus_prefers_live_portfolio_over_stale_consensus():
    portfolio = make_portfolio(
        positions=(
            PositionExposure(
                symbol="BTCUSDT",
                notional_usd=5_000.0,
                leverage=5.0,
                side="LONG",
                risk_amount_usd=100.0,
            ),
        )
    )
    opportunity = make_opportunity(
        agent_consensus={
            "position_risk_pct": {"BTCUSDT": 2.0},
            "position_sides": {"BTCUSDT": "SHORT"},
        }
    )
    result = _portfolio_consensus(portfolio, opportunity)
    assert result["position_risk_pct"]["BTCUSDT"] == pytest.approx(1.0)
    assert result["position_sides"]["BTCUSDT"] == "LONG"


def test_capital_gateway_snapshot_preserves_candidate_and_existing_position_sides():
    opportunity = make_opportunity(
        symbol="ETHUSDT",
        side=TradeSide.SHORT,
        agent_consensus={
            "position_risk_pct": {"BTCUSDT": 1.5},
            "position_sides": {"BTCUSDT": "LONG"},
            "correlations": {"BTCUSDT:ETHUSDT": 0.9},
        },
    )
    gateway = CapitalGateway()
    estimate = gateway.estimate_opportunity(opportunity)
    snapshot = gateway._snapshot(10_000.0, estimate)
    assert estimate.metadata["side"] == "SHORT"
    assert snapshot.position_sides == {"BTCUSDT": "LONG"}
    assert snapshot.correlation("BTCUSDT", "ETHUSDT", 0.75) == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_closed_trade_updates_probability_calibration_once(
    event_bus, risk_engine
):
    install_capital_guard()
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    assert trade.state == TradeLifecycleState.POSITION_OPENED
    gateway = lifecycle._aitos_capital_gateway
    calibrator = gateway.calibrator
    samples_before = calibrator.samples

    closed = await lifecycle.close_trade(trade.trade_id, 101.0, "test_close")
    assert closed.state == TradeLifecycleState.POSITION_CLOSED
    assert calibrator.samples == samples_before + 1
    assert (
        calibrator.observe(
            closed.agent_consensus["capital_objective"]["predicted_loss_probability"],
            closed.pnl <= 0.0,
            observation_id=closed.trade_id,
        )
        is False
    )
    assert calibrator.samples == samples_before + 1
    await lifecycle.shutdown()


@pytest.mark.asyncio
async def test_late_fee_adjustment_revises_closed_trade_calibration(
    event_bus, risk_engine, tmp_path
):
    install_capital_guard()
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    assert trade.state == TradeLifecycleState.POSITION_OPENED
    gateway = lifecycle._aitos_capital_gateway
    from aitos.intelligence.capital_controls import ProbabilityCalibrator

    gateway.calibrator.close()
    calibrator = ProbabilityCalibrator(
        CapitalControlConfig(calibration_min_samples=1),
        path=tmp_path / "late-settlement-calibration.sqlite3",
    )
    gateway.calibrator = calibrator

    closed = await lifecycle.close_trade(trade.trade_id, 101.0, "test_close")
    predicted = closed.agent_consensus["capital_objective"][
        "predicted_loss_probability"
    ]
    assert closed.pnl > 0.0
    assert calibrator.calibrate(predicted) == pytest.approx(0.0)

    await lifecycle.apply_cost_adjustment(
        closed.trade_id,
        adjustment_id="late-fee-1",
        kind="fee",
        amount_usd=100_000.0,
    )
    assert closed.pnl < 0.0
    assert calibrator.samples == 1
    assert calibrator.calibrate(predicted) == pytest.approx(1.0)

    await lifecycle.apply_cost_adjustment(
        closed.trade_id,
        adjustment_id="late-fee-1",
        kind="fee",
        amount_usd=100_000.0,
    )
    assert calibrator.calibrate(predicted) == pytest.approx(1.0)
    await lifecycle.shutdown()


@pytest.mark.asyncio
async def test_open_position_retains_capital_reservation_until_close(
    event_bus, risk_engine, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        capital_runtime,
        "get_model_data_dir",
        lambda: tmp_path,
    )
    install_capital_guard()
    lifecycle = TradeLifecycle(event_bus=event_bus, risk_engine=risk_engine)
    await lifecycle.initialize({})
    trade = await lifecycle.submit_opportunity(make_opportunity(), make_portfolio())
    assert trade.state == TradeLifecycleState.POSITION_OPENED
    gateway = lifecycle._aitos_capital_gateway
    reservation = gateway.reservation.get(trade.symbol)
    assert reservation is not None
    assert reservation.capital_usd > 0.0

    await lifecycle.update_price(trade.trade_id, 100.5)
    assert gateway.reservation.get(trade.symbol) is not None

    await lifecycle.close_trade(trade.trade_id, 101.0, "test_close")
    assert gateway.reservation.get(trade.symbol) is None
    await lifecycle.shutdown()
