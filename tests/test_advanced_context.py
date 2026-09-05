from datetime import datetime, timedelta, timezone

from aitos.intelligence.advanced_context import (
    build_advanced_context,
    price_imbalance,
    structural_symmetry,
    volume_profile,
)
from aitos.models.market import Kline


def _klines(n=80):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out = []
    price = 100.0
    for i in range(n):
        # deterministic alternating legs provide both profile and symmetry data
        phase = (i // 8) % 2
        delta = 1.2 if phase == 0 else -1.0
        open_ = price
        close = price + delta
        high = max(open_, close) + 0.4
        low = min(open_, close) - 0.4
        out.append(
            Kline(
                "TESTUSDT",
                "15m",
                start + timedelta(minutes=15 * i),
                start + timedelta(minutes=15 * (i + 1)),
                open_,
                high,
                low,
                close,
                100.0 + i,
                10000.0,
                100,
                55.0 if delta > 0 else 45.0,
                5500.0 if delta > 0 else 4500.0,
            )
        )
        price = close
    return out


def test_volume_profile_returns_price_location_and_value_area():
    context = volume_profile(_klines())
    assert context is not None
    assert context.val <= context.poc <= context.vah
    assert 0.0 <= context.price_location <= 1.0


def test_price_imbalance_is_bounded():
    context = price_imbalance(_klines())
    assert 0.0 <= context.displacement_score <= 1.0
    assert len(context.zones) <= 8


def test_structural_symmetry_is_optional_and_normalized():
    context = structural_symmetry(_klines())
    assert context is None or 0.0 <= context.similarity <= 1.0
    if context:
        assert context.scale > 0
        assert context.failure_distance >= 0


def test_advanced_context_exposes_machine_readable_features():
    context = build_advanced_context(_klines(), current_cvd_score=7.0, oi_change=0.02)
    assert set(context.features) >= {
        "volume_profile_location",
        "volatility_percentile",
        "imbalance_displacement",
        "forced_flow_pressure",
        "symmetry_similarity",
    }
