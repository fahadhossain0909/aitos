from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aitos.market_data.binance_spot_adapter import _event
from aitos.market_data.contracts import MarketEventType, MarketSource
from aitos.spot.market_state import SpotMarketDataConsumer
from aitos.spot.scanner import SpotOpportunityScanner


def _spot_event(kind, payload, sequence=None, at=None):
    return _event(
        "BTCUSDT",
        kind,
        at or datetime.now(timezone.utc),
        payload,
        MarketSource.WEBSOCKET,
        sequence,
    )


@pytest.mark.asyncio
async def test_spot_live_state_builds_fresh_candidate_from_market_streams():
    scanner = SpotOpportunityScanner(min_quote_volume=Decimal("1000"))
    consumer = SpotMarketDataConsumer(scanner)
    now = datetime.now(timezone.utc)

    await consumer._on_book_snapshot(
        _event(
            "BTCUSDT",
            MarketEventType.BOOK_SNAPSHOT,
            now,
            {
                "last_update_id": 10,
                "bids": [{"price": "100", "quantity": "10"}],
                "asks": [{"price": "100.1", "quantity": "5"}],
            },
            MarketSource.REST,
            10,
        )
    )
    await consumer._on_ticker(
        _spot_event(
            MarketEventType.TICKER,
            {
                "bid_price": "100",
                "bid_quantity": "10",
                "ask_price": "100.1",
                "ask_quantity": "5",
            },
            at=now,
        )
    )
    for index in range(21):
        close = Decimal("100") + Decimal(index) / Decimal("100")
        await consumer._on_kline(
            _spot_event(
                MarketEventType.KLINE,
                {
                    "is_closed": True,
                    "close": str(close),
                    "quote_volume": "200" if index == 20 else "100",
                },
                sequence=index,
                at=now,
            )
        )

    candidate = consumer.build_candidate(
        "BTCUSDT",
        quote_volume_24h=Decimal("500000"),
        now=now,
    )
    assert candidate is not None
    assert candidate.relative_volume == Decimal("2")
    assert candidate.momentum_bps > 0
    assert candidate.order_book_imbalance > 0
    assert consumer.rank_candidates({"BTCUSDT": Decimal("500000")})


@pytest.mark.asyncio
async def test_spot_live_state_rejects_stale_and_invalid_order_books():
    consumer = SpotMarketDataConsumer(SpotOpportunityScanner())
    now = datetime.now(timezone.utc)
    await consumer._on_book_snapshot(
        _event(
            "BTCUSDT",
            MarketEventType.BOOK_SNAPSHOT,
            now,
            {
                "last_update_id": 10,
                "bids": [{"price": "100", "quantity": "2"}],
                "asks": [{"price": "101", "quantity": "2"}],
            },
            MarketSource.REST,
            10,
        )
    )
    await consumer._on_ticker(
        _spot_event(
            MarketEventType.TICKER,
            {
                "bid_price": "100",
                "bid_quantity": "2",
                "ask_price": "101",
                "ask_quantity": "2",
            },
            at=now,
        )
    )
    state = consumer.state("BTCUSDT")
    assert state is not None
    await consumer._on_book_delta(
        _spot_event(
            MarketEventType.BOOK_DELTA,
            {
                "first_update_id": 11,
                "final_update_id": 12,
                "previous_update_id": None,
                "bids": [{"price": "100", "quantity": "3"}],
                "asks": [],
            },
            sequence=12,
            at=now,
        )
    )
    assert state.last_update_id == 12
    await consumer._on_book_delta(
        _spot_event(
            MarketEventType.BOOK_DELTA,
            {
                "first_update_id": 13,
                "final_update_id": 14,
                "previous_update_id": 9,
                "bids": [],
                "asks": [],
            },
            sequence=14,
            at=now,
        )
    )
    assert state.book_valid is False
    assert state.sequence_gaps == 1
    assert (
        consumer.build_candidate(
            "BTCUSDT",
            quote_volume_24h=Decimal("100000"),
            now=now + timedelta(seconds=30),
        )
