from datetime import datetime, timedelta, timezone

from aitos.intelligence.amt import (build_volume_profile,
                                    compute_profile_features)
from aitos.models.market import TradeSide, TradeTick


def _trades(prices, quantities=None):
    quantities = quantities or [1.0] * len(prices)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        TradeTick(
            "BTCUSDT",
            i,
            float(p),
            float(q),
            TradeSide.BUY,
            False,
            start + timedelta(seconds=i),
        )
        for i, (p, q) in enumerate(zip(prices, quantities))
    ]


def test_profile_features_expose_developing_poc_and_nodes():
    profile = build_volume_profile(_trades([100, 101, 101, 102, 102, 102, 103]), 1.0)
    features = compute_profile_features(profile)
    assert features.developing_poc == profile.poc
    assert isinstance(features.hvn, tuple)
    assert isinstance(features.lvn, tuple)
    assert isinstance(features.single_prints, tuple)


def test_old_poc_is_naked_when_not_traded_in_current_profile():
    old = build_volume_profile(_trades([100, 100, 101]), 1.0)
    current = build_volume_profile(_trades([103, 104, 104]), 1.0)
    features = compute_profile_features(current, [old])
    assert old.poc in features.naked_poc
