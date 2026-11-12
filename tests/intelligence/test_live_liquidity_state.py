"""Tests for bounded structural liquidity snapshots in live state."""

from datetime import datetime, timedelta, timezone

from aitos.intelligence.liquidity_intelligence import (
    LiquidityLevel,
    LiquiditySide,
    LiquiditySource,
)
from aitos.intelligence.live_state import LiveMarketStateStore


def _pool(level_id: str, symbol: str, price: float, strength: float) -> LiquidityLevel:
    return LiquidityLevel(
        level_id=level_id,
        symbol=symbol,
        side=LiquiditySide.SELL_SIDE,
        price=price,
        source=LiquiditySource.EQUAL_LOWS,
        timeframe="15m",
        strength=strength,
        touch_count=2,
    )


def test_live_state_stores_bounded_levels_and_filters_other_symbols():
    store = LiveMarketStateStore(max_liquidity_levels=1)
    selected = store.update_liquidity_levels(
        "BTCUSDT",
        [
            _pool("weak", "BTCUSDT", 99000.0, 40.0),
            _pool("strong", "BTCUSDT", 98500.0, 90.0),
            _pool("other", "ETHUSDT", 3000.0, 99.0),
        ],
    )
    snapshot = store.snapshot_model("BTCUSDT")
    assert [level.level_id for level in selected] == ["strong"]
    assert snapshot.liquidity_levels == selected


def test_live_state_expires_stale_structural_liquidity_levels():
    store = LiveMarketStateStore(liquidity_level_max_age_seconds=60)
    store.update_liquidity_levels(
        "BTCUSDT",
        [_pool("stale", "BTCUSDT", 99000.0, 80.0)],
        updated_at=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    assert store.snapshot_model("BTCUSDT").liquidity_levels == ()


def test_live_state_empty_update_clears_previous_levels():
    store = LiveMarketStateStore()
    store.update_liquidity_levels(
        "BTCUSDT", [_pool("pool", "BTCUSDT", 99000.0, 80.0)]
    )
    store.update_liquidity_levels("BTCUSDT", [])
    assert store.snapshot_model("BTCUSDT").liquidity_levels == ()
