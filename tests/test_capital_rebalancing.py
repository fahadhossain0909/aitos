from aitos.intelligence.capital_rebalancing import (
    DynamicRebalancer,
    IncumbentPosition,
    RebalanceCandidate,
    RebalanceConfig,
    RebalanceContext,
)


def _incumbent(**overrides):
    values = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "remaining_net_edge_pct": 0.10,
        "hold_seconds": 900,
        "stagnation_score": 0.85,
        "thesis_health": "degraded",
        "exit_fee_bps": 4,
        "exit_slippage_bps": 3,
    }
    values.update(overrides)
    return IncumbentPosition(**values)


def _candidate(**overrides):
    values = {
        "symbol": "ETHUSDT",
        "side": "SHORT",
        "expected_gross_return_pct": 1.20,
        "expected_loss_pct": 0.50,
        "loss_probability": 0.10,
        "fee_bps": 10,
        "slippage_bps": 5,
        "funding_bps": 0,
        "confidence": 0.95,
        "liquidity_score": 8,
        "risk_pct": 0.50,
        "liquidation_distance_pct": 5.0,
    }
    values.update(overrides)
    return RebalanceCandidate(**values)


def _context(**overrides):
    values = {
        "portfolio_risk_after_incumbent_close_pct": 2.0,
        "candidate_correlated_incremental_risk_pct": 0.5,
        "drawdown_pct": 1.0,
        "available_capital_after_incumbent_close_usd": 1_000,
        "candidate_capital_usd": 500,
        "seconds_since_last_switch": 3_600,
        "switches_last_hour": 0,
    }
    values.update(overrides)
    return RebalanceContext(**values)


def test_high_conviction_candidate_switches_only_after_cost_adjusted_advantage():
    decision = DynamicRebalancer().evaluate(_incumbent(), _candidate(), _context())
    assert decision.should_switch
    assert decision.net_advantage_pct > 0.15
    assert decision.incumbent_exit_cost_pct == 0.07


def test_switch_is_vetoed_when_incumbent_thesis_is_healthy():
    decision = DynamicRebalancer().evaluate(
        _incumbent(stagnation_score=0.1, thesis_health="intact"),
        _candidate(),
        _context(),
    )
    assert not decision.should_switch
    assert "incumbent_thesis_or_momentum_still_healthy" in decision.reasons


def test_switch_is_vetoed_when_fees_destroy_incremental_advantage():
    decision = DynamicRebalancer().evaluate(
        _incumbent(remaining_net_edge_pct=1.0),
        _candidate(),
        _context(),
    )
    assert not decision.should_switch
    assert "net_advantage_below_switch_threshold" in decision.reasons


def test_switch_is_vetoed_by_portfolio_risk_drawdown_or_liquidation_buffer():
    decision = DynamicRebalancer().evaluate(
        _incumbent(),
        _candidate(liquidation_distance_pct=0.2),
        _context(
            portfolio_risk_after_incumbent_close_pct=4.8,
            candidate_correlated_incremental_risk_pct=0.5,
        ),
    )
    assert not decision.should_switch
    assert "candidate_liquidation_buffer_too_small" in decision.reasons
    assert "portfolio_risk_limit" in decision.reasons


def test_switch_is_vetoed_by_churn_controls_and_insufficient_capital():
    decision = DynamicRebalancer().evaluate(
        _incumbent(),
        _candidate(),
        _context(
            seconds_since_last_switch=10,
            switches_last_hour=2,
            available_capital_after_incumbent_close_usd=100,
        ),
    )
    assert not decision.should_switch
    assert "switch_cooldown_active" in decision.reasons
    assert "hourly_switch_limit" in decision.reasons
    assert "insufficient_available_capital" in decision.reasons


def test_switch_requires_strict_candidate_confidence():
    decision = DynamicRebalancer().evaluate(
        _incumbent(),
        _candidate(confidence=0.84),
        _context(),
    )
    assert not decision.should_switch
    assert "candidate_confidence_below_threshold" in decision.reasons


def test_configuration_rejects_invalid_thresholds():
    try:
        RebalanceConfig(min_candidate_confidence=1.5)
    except ValueError as exc:
        assert "min_candidate_confidence" in str(exc)
    else:
        raise AssertionError("invalid confidence threshold accepted")


def test_non_positive_candidate_edge_and_excess_loss_probability_are_vetoed():
    decision = DynamicRebalancer().evaluate(
        _incumbent(),
        _candidate(expected_gross_return_pct=0.05),
        _context(),
    )
    assert not decision.should_switch
    assert "candidate_net_edge_non_positive" in decision.reasons

    decision = DynamicRebalancer().evaluate(
        _incumbent(),
        _candidate(loss_probability=0.50),
        _context(),
    )
    assert not decision.should_switch
    assert "candidate_loss_probability_above_limit" in decision.reasons



def test_drawdown_hard_stop_prevents_switch_even_with_large_advantage():
    decision = DynamicRebalancer().evaluate(
        _incumbent(),
        _candidate(),
        _context(drawdown_pct=10.0),
    )
    assert not decision.should_switch
    assert "drawdown_protection_stop" in decision.reasons


def test_default_config_has_valid_loss_probability_threshold():
    assert RebalanceConfig().max_candidate_loss_probability == 0.35


def test_invalid_loss_probability_threshold_is_rejected():
    try:
        RebalanceConfig(max_candidate_loss_probability=1.01)
    except ValueError as exc:
        assert "max_candidate_loss_probability" in str(exc)
    else:
        raise AssertionError("invalid loss probability threshold accepted")


def test_symbol_comparison_is_case_insensitive():
    decision = DynamicRebalancer().evaluate(
        _incumbent(symbol="btcusdt"),
        _candidate(symbol="BTCUSDT"),
        _context(),
    )
    assert not decision.should_switch
    assert "same_instrument" in decision.reasons
