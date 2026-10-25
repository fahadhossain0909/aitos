"""Regression tests for bounded historical order-book persistence."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aitos.config.tuning import load_tuning_settings
from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource
from aitos.market_data.persistence_sink import CanonicalMarketDataPersistenceSink


class FakeRepository:
    def __init__(self):
        self.snapshots = []

    async def save_order_book_snapshots(self, snapshots):
        self.snapshots.extend(snapshots)

    async def save_trade_ticks(self, _trades):
        return None


@pytest.mark.parametrize(
    "profile", ["low_resource", "balanced", "high_performance"]
)
def test_all_tuning_profiles_default_historical_orderbook_depth_to_100(profile):
    settings = load_tuning_settings(profile=profile, mode="paper")

    assert settings.storage.historical_orderbook_levels == 100


@pytest.mark.asyncio
async def test_persistence_sink_truncates_saved_snapshots_to_configured_depth():
    repository = FakeRepository()
    sink = CanonicalMarketDataPersistenceSink(
        event_bus=None,
        repository=repository,
        historical_orderbook_levels=100,
    )
    timestamp = datetime.now(timezone.utc)
    event = MarketEvent(
        event_type=MarketEventType.BOOK_SNAPSHOT,
        exchange="binance",
        market="usd_m_futures",
        symbol="BTCUSDT",
        event_time=timestamp,
        source=MarketSource.WEBSOCKET,
        payload={
            "symbol": "BTCUSDT",
            "last_update_id": 123,
            "timestamp": timestamp.isoformat(),
            "bid_levels": [
                {"price": 1000.0 - index, "qty": 1.0} for index in range(150)
            ],
            "ask_levels": [
                {"price": 1001.0 + index, "qty": 1.0} for index in range(150)
            ],
        },
    )

    await sink._persist_batch([event])

    assert len(repository.snapshots) == 1
    saved = repository.snapshots[0]
    assert len(saved.bids) == 100
    assert len(saved.asks) == 100
    assert saved.best_bid == 1000.0
    assert saved.best_ask == 1001.0
