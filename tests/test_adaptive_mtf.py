from aitos.intelligence.adaptive_mtf import (
    DirectionalEvidence,
    TimeframeContext,
    directional_diagnostics,
    estimate_net_expectancy,
    evaluate_adaptive_htf,
    portfolio_directional_exposure,
    volatility_risk_multiplier,
    walk_forward_metrics,
)
from aitos.models.trade import TradeSide


def test_directional_diagnostics_exposes_mirrored_short_path():
    result = directional_diagnostics("bearish_bos", 3.0, structure_bias="bearish")
    assert result["short"]["structure_pass"]
    assert result["short"]["flow_pass"]
    assert result["long"]["bias_conflict"]


def test_htf_aligned_long_is_accepted():
    decision = evaluate_adaptive_htf(
        TradeSide.LONG,
        [TimeframeContext("4h", "bullish", 8.0)],
        ltf=DirectionalEvidence(structure=8, order_flow=8, momentum=7),
    )
    assert decision.accepted
    assert decision.mode == "aligned"


def test_htf_countertrend_requires_structure_and_flow_confirmation():
    contexts = [TimeframeContext("4h", "bullish", 8.0)]
    weak = evaluate_adaptive_htf(
        TradeSide.SHORT, contexts,
        ltf=DirectionalEvidence(structure=4, order_flow=2, momentum=8),
    )
    strong = evaluate_adaptive_htf(
        TradeSide.SHORT, contexts,
        ltf=DirectionalEvidence(structure=2, order_flow=2, momentum=8),
    )
    assert not weak.accepted
    assert strong.accepted
    assert strong.mode == "countertrend_reversal"


def test_neutral_htf_does_not_automatically_reject_quality_setup():
    decision = evaluate_adaptive_htf(
        TradeSide.SHORT,
        [TimeframeContext("1h", "neutral", 2.0)],
        ltf=DirectionalEvidence(structure=2, order_flow=2, momentum=8),
    )
    assert decision.accepted
    assert decision.mode == "neutral_or_mixed"


def test_missing_htf_is_not_silently_treated_as_neutral():
    decision = evaluate_adaptive_htf(
        TradeSide.LONG,
        [TimeframeContext("4h", "unknown", 0, available=False)],
        ltf=DirectionalEvidence(),
    )
    assert not decision.accepted
    assert decision.reason == "htf_context_unavailable"


def test_expectancy_accounts_for_fees_and_slippage_in_r():
    result = estimate_net_expectancy(0.55, 2.0, costs_r=0.1)
    assert result.net_expectancy_r == 0.55


def test_portfolio_exposure_is_directional_and_gross_aware():
    result = portfolio_directional_exposure([
        {"side": "LONG", "notional_usd": 100},
        {"side": "SHORT", "notional_usd": 40},
    ])
    assert result["gross_notional_usd"] == 140
    assert result["net_directional_usd"] == 60


def test_volatility_dampener_never_increases_risk():
    assert volatility_risk_multiplier(100) <= volatility_risk_multiplier(0)


def test_walk_forward_metrics_include_drawdown_and_expectancy():
    result = walk_forward_metrics([
        {"net_pnl": 10}, {"net_pnl": -4}, {"net_pnl": 6},
    ])
    assert result["trade_count"] == 3
    assert result["win_rate"] == 2 / 3
    assert result["expectancy"] == 4
    assert result["max_drawdown"] == 4
