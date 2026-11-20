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
        now_ns=1_795_000_000_000_000_000,
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
            leverage=1.0,
        )
    }
    return cycle, context, plans


def test_shadow_strategy_candidate_becomes_typed_canonical_opportunity():
    cycle, context, plans = inputs()
    gateway = ShadowGateway()
    report = build_risk_governed_opportunities(
        cycle,
        context,
        plans,
        shadow_gateway_factory=lambda: gateway,
        equity_usd=1_000.0,
        live_reservation=object(),
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
    cycle, context, plans = inputs()
    live = ShadowGateway()
    with pytest.raises(ValueError, match="share the live reservation"):
        build_risk_governed_opportunities(
            cycle,
            context,
            plans,
            shadow_gateway_factory=lambda: live,
            equity_usd=1_000.0,
            live_reservation=live.reservation,
        )


def test_capital_veto_produces_no_canonical_opportunity():
    cycle, context, plans = inputs()
    report = build_risk_governed_opportunities(
        cycle,
        context,
        plans,
        shadow_gateway_factory=lambda: ShadowGateway(eligible=False),
        equity_usd=1_000.0,
        live_reservation=object(),
    )
    assert report.opportunities == ()
    assert report.rejected == (("amt-flow:binance:futures:BTCUSDT", "portfolio_risk_limit"),)


@pytest.mark.parametrize(
    "plan",
    [
        StrategyProtectionPlan(101.0, (110.0,)),
        StrategyProtectionPlan(95.0, (99.0,)),
        StrategyProtectionPlan(95.0, ()),
        StrategyProtectionPlan(95.0, (110.0,), leverage=0.0),
    ],
)
def test_missing_or_directionally_invalid_protection_is_rejected(plan):
    cycle, context, plans = inputs()
    plans["binance:futures:BTCUSDT"] = plan
    report = build_risk_governed_opportunities(
        cycle,
        context,
        plans,
        shadow_gateway_factory=ShadowGateway,
        equity_usd=1_000.0,
        live_reservation=object(),
    )
    assert report.opportunities == ()


def test_live_or_paper_context_cannot_enter_shadow_opportunity_bridge():
    cycle, context, plans = inputs(mode=StrategyMode.LIVE)
    with pytest.raises(ValueError, match="SHADOW"):
        build_risk_governed_opportunities(
            cycle,
            context,
            plans,
            shadow_gateway_factory=ShadowGateway,
            equity_usd=1_000.0,
            live_reservation=object(),
        )
