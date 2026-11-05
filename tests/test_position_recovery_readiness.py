"""Fail-closed startup gates for recovered position market-data coverage."""

import pytest

from aitos.trading.persistent_state import restored_position_feeds_ready


def test_empty_portfolio_does_not_require_position_specific_feeds():
    assert restored_position_feeds_ready(0, None)


def test_restored_positions_require_at_least_one_registered_ingestion():
    assert not restored_position_feeds_ready(
        2,
        {"symbols": ["BTCUSDT"], "ingestions": 0, "verified": 0, "failures": 1},
    )


def test_restored_positions_require_every_ingestion_to_be_verified():
    assert not restored_position_feeds_ready(
        2,
        {"symbols": ["BTCUSDT"], "ingestions": 2, "verified": 1, "failures": 0},
    )


def test_any_subscription_failure_blocks_startup():
    assert not restored_position_feeds_ready(
        1,
        {"symbols": ["BTCUSDT"], "ingestions": 1, "verified": 1, "failures": 1},
    )


def test_verified_trade_and_kline_coverage_allows_startup():
    assert restored_position_feeds_ready(
        2,
        {
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "ingestions": 2,
            "verified": 2,
            "failures": 0,
        },
    )


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        {"symbols": [], "ingestions": 1, "verified": 1, "failures": 0},
        {"symbols": ["BTCUSDT"], "ingestions": "invalid", "verified": 1, "failures": 0},
        {"symbols": ["BTCUSDT"], "ingestions": 1, "verified": 0, "failures": 0},
    ],
)
def test_malformed_or_incomplete_recovery_evidence_fails_closed(result):
    assert not restored_position_feeds_ready(1, result)
