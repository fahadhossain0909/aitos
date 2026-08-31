from aitos.backtest.hedge_metrics import compare, excursions, expectancy


def test_hedge_metrics_and_comparison():
    baseline_equity = (10000.0, 9900.0, 9700.0, 9800.0, 10200.0, 10500.0)
    hedged_equity = (10000.0, 9950.0, 9850.0, 9900.0, 10300.0, 10520.0)
    baseline_pnls = (-100.0, -200.0, 500.0, 400.0)
    hedged_pnls = (-50.0, -100.0, 400.0, 520.0)
    baseline_ex = [excursions(100.0, "LONG", (99.0, 97.0, 102.0, 105.0))]
    hedged_ex = [excursions(100.0, "LONG", (99.5, 98.5, 103.0, 105.2))]

    result = compare(
        baseline_equity,
        hedged_equity,
        baseline_pnls,
        hedged_pnls,
        baseline_ex,
        hedged_ex,
        hedge_pnl=70.0,
        hedge_cost=10.0,
        hedge_count=2,
    )

    assert result.baseline_net_pnl == 500.0
    assert result.hedged_net_pnl == 520.0
    assert result.baseline_max_drawdown == 0.03
    assert result.hedged_max_drawdown == 0.015
    assert result.baseline_mae == -0.03
    assert result.hedged_mae == -0.015
    assert result.baseline_mfe == 0.05
    assert result.hedged_mfe == 0.052
    assert result.hedge_cost == 10.0
    assert result.hedge_pnl == 70.0
    assert expectancy(baseline_pnls) == 150.0
    assert expectancy(hedged_pnls) == 192.5
    assert result.pnl_delta == 20.0
    assert result.drawdown_reduction == 0.5
