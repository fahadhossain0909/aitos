import pytest

from aitos.world_intelligence.propagation import (
    PropagationSample,
    validate_event_propagation,
)


def test_cross_asset_propagation_estimates_pairwise_regime_lead_lag():
    rows = [
        PropagationSample(
            event_id=f"event-{i}",
            source_asset="BTCUSDT",
            target_asset="ETHUSDT",
            regime="risk_on",
            source_return=(i - 15) / 1000,
            target_return=2 * (i - 15) / 1000 + 0.0001,
            lag_seconds=60 + i % 3,
        )
        for i in range(50)
    ]
    report = validate_event_propagation(rows, min_samples=30, iterations=200)
    assert len(report) == 1
    estimate = report[0]
    assert estimate.sample_count == 50
    assert estimate.return_correlation > 0.99
    assert estimate.correlation_ci[0] > 0.99
    assert estimate.lead_lag_label == "source_leads_target"
    assert estimate.lag_ci[0] > 0


def test_propagation_does_not_pool_regimes_and_rejects_duplicate_events():
    rows = [
        PropagationSample(f"e{i}", "BTC", "ETH", "trend", i / 100, i / 200, 5)
        for i in range(30)
    ] + [
        PropagationSample(f"x{i}", "BTC", "ETH", "range", i / 100, -i / 200, -5)
        for i in range(30)
    ]
    report = validate_event_propagation(rows, min_samples=20, iterations=100)
    assert {r.regime for r in report} == {"trend", "range"}
    with pytest.raises(ValueError, match="duplicate"):
        validate_event_propagation([rows[0], rows[0]], min_samples=3, iterations=100)
