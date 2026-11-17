from datetime import datetime, timedelta, timezone

import pytest

from aitos.capital_intelligence.contracts import CapitalEvent
from aitos.capital_intelligence.replay import (
    PriceObservation,
    build_point_in_time_replay,
    chronological_split,
    summarize_replay,
)


def _event(event_id, at, *, direction=1, finality="confirmed", usd_value=1000):
    return CapitalEvent(
        event_id,
        "fixture",
        "capital_flow",
        "crypto_spot",
        "binance",
        "BTCUSDT",
        "large_transfer",
        at,
        at + timedelta(seconds=1),
        1,
        "BTC",
        usd_value,
        actor_id="wallet-a",
        finality=finality,
        metadata={"flow_sign": direction},
    )


def test_replay_uses_only_point_in_time_prices_and_directional_labels():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [_event("in", start), _event("out", start + timedelta(seconds=600), direction=-1)]
    prices = [
        PriceObservation("BTCUSDT", start, 100),
        PriceObservation("BTCUSDT", start + timedelta(seconds=300), 110),
        PriceObservation("BTCUSDT", start + timedelta(seconds=600), 120),
        PriceObservation("BTCUSDT", start + timedelta(seconds=900), 108),
    ]
    rows = build_point_in_time_replay(
        events, prices, horizons_seconds=(300,), max_price_age_seconds=1
    )
    assert len(rows) == 2
    assert rows[0].forward_returns[300] == pytest.approx(0.1)
    assert rows[1].forward_returns[300] == pytest.approx(0.1)
    summary = summarize_replay(rows, horizon_seconds=300)
    assert summary.sample_count == 2
    assert summary.hit_rate == 1.0


def test_replay_excludes_reverted_unknown_direction_and_stale_prices():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        _event("reverted", start, finality="reverted"),
        _event("unknown", start + timedelta(seconds=1), direction=0),
        _event("stale", start + timedelta(seconds=10)),
    ]
    prices = [
        PriceObservation("BTCUSDT", start, 100),
        PriceObservation("BTCUSDT", start + timedelta(seconds=300), 101),
    ]
    rows = build_point_in_time_replay(
        events, prices, horizons_seconds=(300,), max_price_age_seconds=1
    )
    assert rows == []


def test_chronological_split_keeps_future_rows_out_of_train():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [_event(str(i), start + timedelta(minutes=i)) for i in range(10)]
    prices = [
        PriceObservation("BTCUSDT", start + timedelta(minutes=i), 100 + i)
        for i in range(11)
    ]
    rows = build_point_in_time_replay(
        events, prices, horizons_seconds=(60,), max_price_age_seconds=1
    )
    train, test = chronological_split(rows, test_fraction=0.2)
    assert train and test
    assert max(row.event_time for row in train) < min(row.event_time for row in test)
