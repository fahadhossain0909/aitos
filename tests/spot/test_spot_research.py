import math
from datetime import datetime, timedelta, timezone

import pytest

from aitos.spot.research import (
    align_futures_spot_ticker_events,
    validate_futures_spot_lead_lag_from_clickhouse,
    validate_futures_spot_lead_lag_oos,
)


def test_lead_lag_oos_selects_spot_leader_without_test_lookahead():
    spot = [0.001 * math.sin(i * 1.7) + 0.0004 * math.cos(i * 0.31) for i in range(500)]
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


def test_ticker_event_alignment_uses_contiguous_shared_seconds_only():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    spot_events = [
        {
            "event_time": start + timedelta(seconds=i),
            "payload": {"last_price": str(100 + i)},
        }
        for i in range(8)
    ]
    futures_events = [
        {
            "event_time": start + timedelta(seconds=i),
            "payload": {"bid_price": str(199 + i), "ask_price": str(201 + i)},
        }
        for i in [0, 1, 2, 5, 6, 7]
    ]
    aligned = align_futures_spot_ticker_events(
        spot_events=spot_events,
        futures_events=futures_events,
    )
    assert aligned.aligned_price_buckets == 3
    assert aligned.discarded_unaligned_buckets == 3
    assert len(aligned.spot_returns) == 2
    assert len(aligned.futures_returns) == 2


@pytest.mark.asyncio
async def test_clickhouse_oos_loader_uses_explicit_market_and_instrument_identity():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    returns = [
        0.001 * math.sin(i * 1.7) + 0.0004 * math.cos(i * 0.31) for i in range(400)
    ]
    spot_prices = [100.0]
    futures_returns = [0.0, 0.0] + returns[:-2]
    futures_prices = [100.0]
    for value in returns:
        spot_prices.append(spot_prices[-1] * math.exp(value))
    for value in futures_returns:
        futures_prices.append(futures_prices[-1] * math.exp(value))
    spot_events = [
        {
            "event_time": start + timedelta(seconds=i),
            "payload": {"last_price": str(price)},
        }
        for i, price in enumerate(spot_prices)
    ]
    futures_events = [
        {
            "event_time": start + timedelta(seconds=i),
            "payload": {"last_price": str(price)},
        }
        for i, price in enumerate(futures_prices)
    ]

    class FakeRepository:
        calls = []

        async def load_versioned_market_events(self, **kwargs):
            self.calls.append(kwargs)
            return spot_events if kwargs["market_type"] == "spot" else futures_events

    repository = FakeRepository()
    result = await validate_futures_spot_lead_lag_from_clickhouse(
        repository,
        spot_instrument_id="binance:spot:BTCUSDT",
        futures_instrument_id="BTCUSDT",
        start_time=start,
        end_time=start + timedelta(seconds=401),
        max_lag_seconds=4,
        train_fraction=0.6,
        min_train_observations=100,
        min_test_observations=100,
        min_abs_test_correlation=0.5,
        min_incremental_abs_correlation=0.1,
    )
    assert result.leader == "spot"
    assert result.lag_seconds == 2
    assert result.validated
    assert repository.calls[0]["instrument_id"] == "binance:spot:BTCUSDT"
    assert repository.calls[1]["instrument_id"] == "BTCUSDT"
    assert {call["market_type"] for call in repository.calls} == {
        "spot",
        "usd_m_futures",
    }
