from datetime import datetime, timezone

import pytest

from aitos.statistics.risk_overlay import assess_statistical_risk_forecast


def forecast_payload(**overrides):
    values = {
        "symbol": "BTCUSDT",
        "horizon": "15m",
        "side": "LONG",
        "model_id": "a-stat",
        "model_version": "1",
        "as_of": "2026-11-07T05:00:00+00:00",
        "sample_size": 256,
        "predicted_up": 0.7,
        "downside_probability": 0.2,
        "expected_return": 0.01,
        "forecast_volatility": 0.04,
        "reference_volatility": 0.02,
        "tail_probability": 0.2,
        "expected_shortfall": 0.05,
    }
    values.update(overrides)
    return values


def test_forecast_assessment_requires_matching_identity_and_freshness():
    now = datetime(2026, 11, 7, 5, 0, 10, tzinfo=timezone.utc)
    result = assess_statistical_risk_forecast(
        forecast_payload(),
        symbol="BTCUSDT",
        side="LONG",
        horizon="15m",
        now=now,
    )
    assert 0.0 < result.size_multiplier < 1.0
    assert result.volatility_ratio == pytest.approx(2.0)

    with pytest.raises(ValueError, match="symbol, side or horizon mismatch"):
        assess_statistical_risk_forecast(
            forecast_payload(),
            symbol="ETHUSDT",
            side="LONG",
            horizon="15m",
            now=now,
        )


@pytest.mark.parametrize(
    ("overrides", "now", "message"),
    [
        (
            {"sample_size": 10},
            datetime(2026, 11, 7, 5, 0, 10, tzinfo=timezone.utc),
            "insufficient observations",
        ),
        (
            {"as_of": "2026-11-07T04:00:00+00:00"},
            datetime(2026, 11, 7, 5, 0, 10, tzinfo=timezone.utc),
            "stale",
        ),
        (
            {"as_of": "2026-11-07T05:01:00+00:00"},
            datetime(2026, 11, 7, 5, 0, 10, tzinfo=timezone.utc),
            "future",
        ),
        (
            {"forecast_volatility": float("nan")},
            datetime(2026, 11, 7, 5, 0, 10, tzinfo=timezone.utc),
            "finite",
        ),
        (
            {"predicted_up": 1.2},
            datetime(2026, 11, 7, 5, 0, 10, tzinfo=timezone.utc),
            "predicted_up must be in",
        ),
    ],
)
def test_forecast_assessment_fails_closed(overrides, now, message):
    with pytest.raises(ValueError, match=message):
        assess_statistical_risk_forecast(
            forecast_payload(**overrides),
            symbol="BTCUSDT",
            side="LONG",
            horizon="15m",
            now=now,
        )


def test_forecast_assessment_rejects_naive_timestamps():
    with pytest.raises(ValueError, match="timezone-aware"):
        assess_statistical_risk_forecast(
            forecast_payload(as_of="2026-11-07T05:00:00"),
            symbol="BTCUSDT",
            side="LONG",
            horizon="15m",
            now=datetime(2026, 11, 7, 5, 0, 10, tzinfo=timezone.utc),
        )


def test_forecast_assessment_rejects_opposite_position_side():
    now = datetime(2026, 11, 7, 5, 0, 10, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="symbol, side or horizon mismatch"):
        assess_statistical_risk_forecast(
            forecast_payload(side="SHORT"),
            symbol="BTCUSDT",
            side="LONG",
            horizon="15m",
            now=now,
        )
