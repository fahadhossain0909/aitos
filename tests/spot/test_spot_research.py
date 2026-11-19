import math

import pytest

from aitos.spot.research import validate_futures_spot_lead_lag_oos


def test_lead_lag_oos_selects_spot_leader_without_test_lookahead():
    spot = [
        0.001 * math.sin(i * 1.7) + 0.0004 * math.cos(i * 0.31)
        for i in range(500)
    ]
    futures = [0.0, 0.0] + spot[:-2]
    regimes = ["trend" if i % 2 else "range" for i in range(len(spot))]
    result = validate_futures_spot_lead_lag_oos(
        spot_returns=spot,
        futures_returns=futures,
        regimes=regimes,
        max_lag_seconds=4,
        train_fraction=0.6,
        min_train_observations=100,
        min_test_observations=100,
        min_abs_test_correlation=0.5,
        min_incremental_abs_correlation=0.1,
    )
    assert result.leader == "spot"
    assert result.lag_seconds == 2
    assert result.train_correlation > 0.99
    assert result.test_correlation > 0.99
    assert result.confidence_low > 0.9
    assert result.validated
    assert set(result.regime_correlations) == {"range", "trend"}


def test_lead_lag_oos_rejects_insufficient_history():
    with pytest.raises(ValueError, match="insufficient"):
        validate_futures_spot_lead_lag_oos(
            spot_returns=[0.0] * 20,
            futures_returns=[0.0] * 20,
            min_train_observations=60,
        )
