from aitos.intelligence.capital_objective import (
    CapitalAllocator,
    CapitalObjective,
    CapitalObjectiveConfig,
    OpportunityEstimate,
)


def _estimate(symbol: str, **overrides):
    values = dict(
        symbol=symbol,
        expected_gross_return_pct=1.2,
        expected_loss_pct=0.8,
        loss_probability=0.20,
        fee_bps=4.0,
        slippage_bps=3.0,
        funding_bps=1.0,
        liquidity_score=8.0,
        confidence=0.8,
    )
    values.update(overrides)
    return OpportunityEstimate(**values)


def test_net_edge_includes_all_costs_and_expected_loss():
    estimate = _estimate("BTCUSDT")
    # 1.2 - 0.08 cost - (0.20 * 0.8) = 0.96%
    assert round(estimate.total_cost_pct, 6) == 0.08
    assert round(estimate.expected_net_edge_pct, 6) == 0.96


def test_high_return_does_not_override_protection_limits():
    objective = CapitalObjective(
        CapitalObjectiveConfig(max_loss_probability=0.25, max_expected_loss_pct=1.0)
    )
    decision = objective.evaluate(
        _estimate("RISKY", expected_gross_return_pct=8.0, loss_probability=0.60)
    )
    assert not decision.eligible
    assert "loss_probability_above_limit" in decision.rejection_reasons


def test_flat_market_with_fees_is_rejected():
    objective = CapitalObjective()
    decision = objective.evaluate(
        _estimate("FLAT", expected_gross_return_pct=0.02, fee_bps=8, slippage_bps=5)
    )
    assert not decision.eligible
    assert "net_edge_below_minimum" in decision.rejection_reasons


def test_ranking_prefers_sustainable_net_edge():
    objective = CapitalObjective()
    decisions = objective.rank(
        [
            _estimate("A", expected_gross_return_pct=1.0, loss_probability=0.15),
            _estimate("B", expected_gross_return_pct=1.5, loss_probability=0.30),
            _estimate("C", expected_gross_return_pct=0.02),
        ]
    )
    assert [d.symbol for d in decisions] == ["B", "A"]
    assert all(d.eligible for d in decisions)


def test_allocator_never_exceeds_trade_or_portfolio_risk_caps():
    objective = CapitalObjective(
        CapitalObjectiveConfig(max_trade_risk_pct=1.0, max_portfolio_risk_pct=2.0)
    )
    decisions = objective.rank([_estimate("A"), _estimate("B"), _estimate("C")])
    allocations = CapitalAllocator(objective).allocate(
        10_000, decisions, max_positions=3
    )
    assert allocations
    assert all(a.risk_budget_usd <= 100.0 + 1e-9 for a in allocations)
    assert sum(a.risk_budget_usd for a in allocations) <= 200.0 + 1e-9


def test_no_trade_is_a_valid_result():
    objective = CapitalObjective()
    decisions = objective.rank([_estimate("NONE", expected_gross_return_pct=0.01)])
    assert decisions == []
    assert CapitalAllocator(objective).allocate(10_000, decisions) == []
