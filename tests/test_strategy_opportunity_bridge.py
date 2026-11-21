import time
from types import SimpleNamespace

import pytest

from aitos.intelligence.capital_gateway import CapitalGateway, CapitalGatewayResult
from aitos.intelligence.capital_objective import CapitalAllocation, CapitalDecision
from aitos.models.trade import Opportunity, TradeSide
from aitos.strategy.allocator import Allocation
from aitos.strategy.contracts import (
    CapitalRequest,
    ExecutionIntent,
    MarketSnapshot,
    StrategyContext,
    StrategyFamily,
    StrategyMode,
    StrategyResult,
)
from aitos.strategy.engine import StrategyCycle
from aitos.strategy.opportunity_bridge import (
    StrategyPortfolioRiskContext,
    StrategyProtectionPlan,
    build_risk_governed_opportunities,
)


class ShadowGateway(CapitalGateway):
    def __init__(self, *, eligible=True):
        super().__init__()
        self.eligible = eligible
        self.seen = []

    def estimate_opportunity(self, opportunity, **kwargs):
        self.seen.append((opportunity, kwargs))
        return SimpleNamespace(symbol=opportunity.symbol)

    def evaluate(self, equity_usd, estimates, *, max_positions=3):
        decisions = tuple(
            CapitalDecision(
                estimate.symbol,
                self.eligible,
                80.0,
                90.0,
                85.0,
                1.25,
                0.2,
                () if self.eligible else ("portfolio_risk_limit",),
                ("shadow capital review",),
            )
            for estimate in estimates
        )
        allocations = tuple(
            CapitalAllocation(item.symbol, 80.0, 4.0, 85.0)
            for item in decisions
            if item.eligible
        )
        return CapitalGatewayResult(decisions, allocations)


def inputs(mode=StrategyMode.SHADOW):
    context = StrategyContext(
        now_ns=time.time_ns(),
        mode=mode,
        snapshots={
            "binance:futures:BTCUSDT": MarketSnapshot(
                instrument_id="binance:futures:BTCUSDT",
                mid=100.0,
                liquidity_score=8.0,
            )
        },
        available_capital=100.0,
        risk_budget=5.0,
        global_regime="trending",
    )
    result = StrategyResult(
        strategy_id="amt-flow",
        family=StrategyFamily.DIRECTIONAL,
        intents=(
            ExecutionIntent(
                instrument_id="binance:futures:BTCUSDT",
                side="buy",
                quantity=1.0,
                strategy_id="amt-flow",
                rationale="auction acceptance with positive delta",
            ),
        ),
        capital_request=CapitalRequest(
            strategy_id="amt-flow",
            requested_notional=100.0,
            max_loss=5.0,
            expected_edge=2.0,
            confidence=0.9,
        ),
    )
    cycle = StrategyCycle(
        regime="trending",
        results=(result,),
        allocations=(Allocation("amt-flow", 100.0, 5.0, 1.0),),
    )
    plans = {
        "binance:futures:BTCUSDT": StrategyProtectionPlan(
            stop_loss_price=95.0,
            take_profit_levels=(110.0, 115.0),
            loss_probability=0.15,
            leverage=1.0,
        )
    }
    portfolio_risk = StrategyPortfolioRiskContext(
        snapshot_id="portfolio-snapshot-001",
        observed_at_ns=context.now_ns,
        equity_usd=1_000.0,
        equity_peak_usd=1_000.0,
    )
    return cycle, context, plans, portfolio_risk


def test_shadow_strategy_candidate_becomes_typed_canonical_opportunity():
    cycle, context, plans, portfolio_risk = inputs()
    gateway = ShadowGateway()
    report = build_risk_governed_opportunities(
        cycle,
        context,
        portfolio_risk,
        plans,
        shadow_gateway_factory=lambda: gateway,
        live_gateway=ShadowGateway(),
    )

    assert len(report.opportunities) == 1
    governed = report.opportunities[0]
    opportunity = governed.opportunity
    assert isinstance(opportunity, Opportunity)
    assert opportunity.side is TradeSide.LONG
    assert opportunity.symbol == "BTCUSDT"
    assert opportunity.stop_loss_price == 95.0
    assert opportunity.take_profit_levels == [110.0, 115.0]
    assert opportunity.is_production is False
    assert opportunity.approved_by is None
    assert opportunity.agent_consensus["capital_governance"]["capital_usd"] == 80.0
    assert governed.risk_receipt.risk_budget_usd == 4.0
    assert governed.risk_receipt.execution_authorized is False
    assert governed.execution_eligible is False
    assert report.execution_eligible is False


def test_shadow_gateway_must_not_share_live_reservation_ledger():
    cycle, context, plans, portfolio_risk = inputs()
    live = ShadowGateway()
    with pytest.raises(ValueError, match="share the live reservation"):
        build_risk_governed_opportunities(
            cycle,
            context,
            portfolio_risk,
            plans,
            shadow_gateway_factory=lambda: live,
            live_gateway=live,
        )


def test_capital_veto_produces_no_canonical_opportunity():
    cycle, context, plans, portfolio_risk = inputs()
    report = build_risk_governed_opportunities(
        cycle,
        context,
        portfolio_risk,
        plans,
        shadow_gateway_factory=lambda: ShadowGateway(eligible=False),
        live_gateway=ShadowGateway(),
    )
    assert report.opportunities == ()
    assert report.rejected == (
        ("amt-flow:binance:futures:BTCUSDT", "portfolio_risk_limit"),
    )


@pytest.mark.parametrize(
    "plan",
    [
        StrategyProtectionPlan(101.0, (110.0,), loss_probability=0.15),
        StrategyProtectionPlan(95.0, (99.0,), loss_probability=0.15),
        StrategyProtectionPlan(95.0, (), loss_probability=0.15),
        StrategyProtectionPlan(95.0, (110.0,), loss_probability=0.15, leverage=0.0),
        StrategyProtectionPlan(95.0, (110.0,), loss_probability=1.5),
    ],
)
def test_missing_or_directionally_invalid_protection_is_rejected(plan):
    cycle, context, plans, portfolio_risk = inputs()
    plans["binance:futures:BTCUSDT"] = plan
    report = build_risk_governed_opportunities(
        cycle,
        context,
        portfolio_risk,
        plans,
        shadow_gateway_factory=ShadowGateway,
        live_gateway=ShadowGateway(),
    )
    assert report.opportunities == ()


def test_live_or_paper_context_cannot_enter_shadow_opportunity_bridge():
    cycle, context, plans, portfolio_risk = inputs(mode=StrategyMode.LIVE)
    with pytest.raises(ValueError, match="SHADOW"):
        build_risk_governed_opportunities(
            cycle,
            context,
            portfolio_risk,
            plans,
            shadow_gateway_factory=ShadowGateway,
            live_gateway=ShadowGateway(),
        )


def test_shadow_opportunity_cannot_exceed_strategy_projected_loss_cap():
    cycle, context, plans, portfolio_risk = inputs()
    cycle = StrategyCycle(
        regime=cycle.regime,
        results=cycle.results,
        allocations=(Allocation("amt-flow", 100.0, 2.0, 1.0),),
    )
    report = build_risk_governed_opportunities(
        cycle,
        context,
        portfolio_risk,
        plans,
        shadow_gateway_factory=ShadowGateway,
        live_gateway=ShadowGateway(),
    )
    governed = report.opportunities[0]
    assert governed.risk_receipt.risk_budget_usd == 2.0
    assert governed.risk_receipt.capital_usd == 40.0


def test_multiple_intents_do_not_reuse_one_strategy_allocation():
    cycle, context, plans, portfolio_risk = inputs()
    result = cycle.results[0]
    second = ExecutionIntent(
        instrument_id="binance:futures:ETHUSDT",
        side="buy",
        quantity=1.0,
        strategy_id="amt-flow",
        rationale="second instrument",
    )
    context = StrategyContext(
        now_ns=context.now_ns,
        mode=context.mode,
        snapshots={
            **context.snapshots,
            "binance:futures:ETHUSDT": MarketSnapshot(
                instrument_id="binance:futures:ETHUSDT",
                mid=100.0,
                liquidity_score=8.0,
            ),
        },
        available_capital=context.available_capital,
        risk_budget=context.risk_budget,
        global_regime=context.global_regime,
    )
    cycle = StrategyCycle(
        regime=cycle.regime,
        results=(
            StrategyResult(
                strategy_id=result.strategy_id,
                family=result.family,
                intents=(*result.intents, second),
                capital_request=result.capital_request,
            ),
        ),
        allocations=cycle.allocations,
    )
    plans["binance:futures:ETHUSDT"] = StrategyProtectionPlan(
        stop_loss_price=95.0,
        take_profit_levels=(110.0,),
        loss_probability=0.15,
    )
    report = build_risk_governed_opportunities(
        cycle,
        context,
        portfolio_risk,
        plans,
        shadow_gateway_factory=ShadowGateway,
        live_gateway=ShadowGateway(),
    )
    assert len(report.opportunities) == 1
    assert any("multiple intents" in reason for _, reason in report.rejected)


def test_shadow_gateway_cannot_share_live_sqlite_reservation_database(tmp_path):
    from aitos.intelligence.capital_protection import CapitalReservation

    cycle, context, plans, portfolio_risk = inputs()
    live_gateway = ShadowGateway()
    live_gateway.reservation = CapitalReservation(
        path=tmp_path / "live-capital.sqlite"
    )
    shadow_gateway = ShadowGateway()
    shadow_gateway.reservation = CapitalReservation(
        path=tmp_path / "live-capital.sqlite"
    )
    with pytest.raises(ValueError, match="share the live reservation"):
        build_risk_governed_opportunities(
            cycle,
            context,
            portfolio_risk,
            plans,
            shadow_gateway_factory=lambda: shadow_gateway,
            live_gateway=live_gateway,
        )
    live_gateway.reservation.close()
    shadow_gateway.reservation.close()


def test_real_capital_gateway_reviews_candidate_without_live_authorization():
    cycle, context, plans, portfolio_risk = inputs()
    report = build_risk_governed_opportunities(
        cycle,
        context,
        portfolio_risk,
        plans,
        shadow_gateway_factory=CapitalGateway,
        live_gateway=CapitalGateway(),
    )
    assert len(report.opportunities) == 1
    governed = report.opportunities[0]
    assert governed.risk_receipt.capital_usd > 0
    assert 0 < governed.risk_receipt.risk_budget_usd <= 5.0
    assert governed.opportunity.is_production is False
    assert governed.risk_receipt.execution_authorized is False
    assert report.execution_eligible is False


def test_stale_portfolio_risk_snapshot_fails_closed():
    cycle, context, plans, portfolio_risk = inputs()
    stale = StrategyPortfolioRiskContext(
        snapshot_id=portfolio_risk.snapshot_id,
        observed_at_ns=context.now_ns - 6_000_000_000,
        equity_usd=portfolio_risk.equity_usd,
        equity_peak_usd=portfolio_risk.equity_peak_usd,
    )
    with pytest.raises(ValueError, match="missing, invalid, or stale"):
        build_risk_governed_opportunities(
            cycle,
            context,
            stale,
            plans,
            shadow_gateway_factory=ShadowGateway,
            live_gateway=ShadowGateway(),
        )


def test_typed_portfolio_exposures_are_forwarded_to_capital_gateway():
    cycle, context, plans, _ = inputs()
    portfolio_risk = StrategyPortfolioRiskContext(
        snapshot_id="portfolio-snapshot-with-exposure",
        observed_at_ns=context.now_ns,
        equity_usd=1_000.0,
        equity_peak_usd=1_050.0,
        position_risk_pct={"ETHUSDT": 0.5},
        position_sides={"ETHUSDT": "LONG"},
        position_instruments={"ETHUSDT": "ETHUSDT"},
        pending_order_risk_pct={"pending-sol": 0.2},
        pending_order_symbols={"pending-sol": "SOLUSDT"},
        pending_order_sides={"pending-sol": "SHORT"},
        correlations={"BTCUSDT:ETHUSDT": 0.92},
        daily_pnl_pct=-0.5,
        consecutive_losses=2,
        liquidation_buffer_pct=12.0,
    )
    gateway = ShadowGateway()
    report = build_risk_governed_opportunities(
        cycle,
        context,
        portfolio_risk,
        plans,
        shadow_gateway_factory=lambda: gateway,
        live_gateway=ShadowGateway(),
    )
    assert len(report.opportunities) == 1
    metadata = gateway.seen[0][0].agent_consensus
    assert metadata["position_risk_pct"] == {"ETHUSDT": 0.5}
    assert metadata["pending_order_symbols"] == {"pending-sol": "SOLUSDT"}
    assert metadata["correlations"] == {"BTCUSDT:ETHUSDT": 0.92}
    assert metadata["portfolio_risk_snapshot_id"] == "portfolio-snapshot-with-exposure"


def test_capital_gateway_contract_must_be_typed_before_opportunity_emission():
    class MalformedGateway(ShadowGateway):
        def evaluate(self, equity_usd, estimates, *, max_positions=3):
            return SimpleNamespace(decisions=(), allocations=())

    cycle, context, plans, portfolio_risk = inputs()
    report = build_risk_governed_opportunities(
        cycle,
        context,
        portfolio_risk,
        plans,
        shadow_gateway_factory=MalformedGateway,
        live_gateway=ShadowGateway(),
    )
    assert report.opportunities == ()
    assert any("invalid review contract" in reason for _, reason in report.rejected)


@pytest.mark.parametrize(
    "field,value",
    [
        ("position_risk_pct", {"ETHUSDT": True}),
        ("pending_order_risk_pct", {"pending": True}),
        ("correlations", {"BTCUSDT:ETHUSDT": True}),
        ("liquidation_buffer_pct", True),
    ],
)
def test_boolean_portfolio_risk_evidence_is_rejected(field, value):
    cycle, context, plans, original = inputs()
    values = {
        "snapshot_id": original.snapshot_id,
        "observed_at_ns": original.observed_at_ns,
        "equity_usd": original.equity_usd,
        "equity_peak_usd": original.equity_peak_usd,
    }
    if field == "position_risk_pct":
        values.update(
            position_risk_pct=value,
            position_sides={"ETHUSDT": "LONG"},
            position_instruments={"ETHUSDT": "ETHUSDT"},
        )
    elif field == "pending_order_risk_pct":
        values.update(
            pending_order_risk_pct=value,
            pending_order_symbols={"pending": "ETHUSDT"},
            pending_order_sides={"pending": "LONG"},
        )
    else:
        values[field] = value
    portfolio_risk = StrategyPortfolioRiskContext(**values)
    with pytest.raises(ValueError):
        build_risk_governed_opportunities(
            cycle,
            context,
            portfolio_risk,
            plans,
            shadow_gateway_factory=ShadowGateway,
            live_gateway=ShadowGateway(),
        )
