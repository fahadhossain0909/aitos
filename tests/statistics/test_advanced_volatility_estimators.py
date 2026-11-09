import math

from aitos.statistics.advanced_models import EGARCH11, GJRGARCH11, fit_har_rv_forecast


def test_egarch_estimation_returns_finite_stationary_forecast():
    values = []
    for i in range(800):
        volatility = 0.001 if (i // 100) % 2 == 0 else 0.004
        shock = math.sin(i * 0.37) + 0.3 * math.cos(i * 0.13)
        values.append(volatility * shock)
    fit = EGARCH11().fit_forecast(values)
    assert fit.observations == len(values)
    assert math.isfinite(fit.negative_log_likelihood)
    assert fit.variance > 0.0
    assert fit.volatility > 0.0
    assert abs(fit.beta) < 0.995
    assert fit.long_run_variance > 0.0


def test_gjr_and_har_estimators_produce_finite_forecasts():
    values = [
        0.001 * math.sin(i * 0.23) + 0.0004 * math.cos(i * 0.11) for i in range(300)
    ]
    gjr = GJRGARCH11().fit_forecast(values)
    har = fit_har_rv_forecast(values)
    assert gjr.variance > 0.0
    assert gjr.persistence < 0.995
    assert har.variance >= 0.0
    assert math.isfinite(har.volatility)
