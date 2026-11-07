import math
import random

import pytest

from aitos.statistics.tail_calibration import backtest_var_es, fit_student_t_copula


def test_student_t_copula_estimates_dependence_and_tail_dependence():
    rng = random.Random(20261107)
    first = []
    second = []
    for _ in range(600):
        common = rng.gauss(0.0, 1.0)
        first.append(common + 0.25 * rng.gauss(0.0, 1.0))
        second.append(0.7 * common + 0.5 * rng.gauss(0.0, 1.0))
    fit = fit_student_t_copula(
        {"BTCUSDT": first, "ETHUSDT": second},
        degrees_of_freedom_grid=(3, 4, 6, 10, 20),
    )
    assert fit.observations == 600
    assert math.isfinite(fit.log_likelihood)
    assert 2.0 < fit.degrees_of_freedom <= 20.0
    assert 0.2 < fit.correlation_matrix[0][1] < 0.99
    assert 0.0 < fit.tail_dependence_matrix[0][1] < 1.0


def test_var_es_backtest_reports_coverage_independence_and_tail_severity():
    returns = [0.001 * math.sin(i * 0.37) for i in range(200)]
    for index in (10, 50, 90, 130, 170):
        returns[index] = -0.05
    result = backtest_var_es(
        returns,
        [0.02] * len(returns),
        [0.03] * len(returns),
        confidence=0.975,
    )
    assert result.observations == 200
    assert result.exceptions == 5
    assert result.expected_exceptions == pytest.approx(5.0)
    assert 0.0 <= result.kupiec_p_value <= 1.0
    assert 0.0 <= result.independence_p_value <= 1.0
    assert 0.0 <= result.conditional_coverage_p_value <= 1.0
    assert result.expected_shortfall_ratio == pytest.approx(0.05 / 0.03)


def test_var_es_backtest_rejects_inconsistent_forecasts():
    with pytest.raises(ValueError, match="ES >= VaR"):
        backtest_var_es([0.0] * 40, [0.02] * 40, [0.01] * 40)
