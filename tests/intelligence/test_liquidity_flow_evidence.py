"""Tests for freshness-gated live flow evidence used by sweep classification."""

from datetime import datetime, timezone
from types import SimpleNamespace

from aitos.intelligence.contextual_scanner_bridge import (
    _live_liquidity_flow_evidence,
)


def test_live_flow_evidence_uses_fresh_trade_and_depth_snapshots():
    now = datetime.now(timezone.utc)
    flow = SimpleNamespace(
        timestamp=now,
        trade_count=20,
        buy_ratio=0.8,
    )
    book = SimpleNamespace(
        timestamp=now,
        bids=((100.0, 20.0),),
        asks=((101.0, 10.0),),
    )
    events = (SimpleNamespace(kind="stacking", side="bid", score=4.0),)
    store = SimpleNamespace(
        snapshot_model=lambda symbol: SimpleNamespace(
            order_flow=flow,
            order_book=book,
            liquidity_events=events,
        )
    )
    evidence = _live_liquidity_flow_evidence(store, "BTCUSDT")
    assert evidence.fresh
    assert evidence.confidence == 1.0
    assert evidence.cvd_score == 8.0
    assert evidence.ofi_score > 5.0
    assert evidence.bid_replenishment == 4.0


def test_stale_live_flow_or_book_never_confirms_a_reversal():
    from datetime import timedelta

    stale = datetime.now(timezone.utc) - timedelta(minutes=1)
    flow = SimpleNamespace(timestamp=stale, trade_count=100, buy_ratio=0.9)
    book = SimpleNamespace(
        timestamp=datetime.now(timezone.utc),
        bids=((100.0, 20.0),),
        asks=((101.0, 10.0),),
    )
    store = SimpleNamespace(
        snapshot_model=lambda symbol: SimpleNamespace(
            order_flow=flow,
            order_book=book,
            liquidity_events=(),
        )
    )
    evidence = _live_liquidity_flow_evidence(store, "BTCUSDT")
    assert not evidence.fresh
    assert evidence.confidence == 0.0
