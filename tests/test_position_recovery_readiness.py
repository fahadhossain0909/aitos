"""Fail-closed startup gates for recovered position market-data coverage."""

import pytest

from aitos.trading.persistent_state import restored_position_feeds_ready


def _evidence(*, trade=("BTCUSDT",), kline=("BTCUSDT",), verified=True):
    return {
        "ingestion": "DataIngestionService:0",
        "trade_symbols": list(trade),
        "kline_symbols": list(kline),
        "verified": verified,
    }


def _result(
    *,
    symbols=("BTCUSDT",),
    evidence=None,
    ingestions=1,
    verified=1,
    failures=0,
    malformed=0,
):
    evidence = [_evidence()] if evidence is None else evidence
    return {
        "symbols": list(symbols),
        "ingestions": ingestions,
        "verified": verified,
        "failures": failures,
        "ingestion_evidence": evidence,
        "malformed_trade_symbols": malformed,
    }


def test_empty_portfolio_does_not_require_position_specific_feeds():
    assert restored_position_feeds_ready(0, None, [])


def test_restored_positions_require_at_least_one_registered_ingestion():
    result = _result(evidence=[], ingestions=0, verified=0, failures=1)
    assert not restored_position_feeds_ready(2, result, ["BTCUSDT"])


def test_every_ingestion_must_verify_both_trade_and_kline_symbols():
    result = _result(
        evidence=[
            _evidence(),
            _evidence(trade=("BTCUSDT",), kline=(), verified=False),
        ],
        ingestions=2,
        verified=1,
        failures=1,
    )
    assert not restored_position_feeds_ready(2, result, ["BTCUSDT"])


def test_verified_trade_and_kline_coverage_allows_startup():
    result = _result(
        symbols=("BTCUSDT", "ETHUSDT"),
        evidence=[
            _evidence(
                trade=("BTCUSDT", "ETHUSDT"),
                kline=("BTCUSDT", "ETHUSDT"),
            )
        ],
    )
    assert restored_position_feeds_ready(2, result, ["BTCUSDT", "ETHUSDT"])


def test_expected_restored_symbols_must_match_reported_coverage():
    assert not restored_position_feeds_ready(
        1, _result(symbols=("BTCUSDT",)), ["ETHUSDT"]
    )


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        _result(symbols=()),
        _result(evidence=[]),
        _result(evidence=[_evidence(kline=(), verified=False)], verified=0, failures=1),
        _result(evidence=[_evidence(trade=(), verified=False)], verified=0, failures=1),
        _result(malformed=1),
        _result(ingestions=True),
        _result(verified=False),
        _result(failures=False),
        _result(ingestions=-1),
        _result(evidence=[_evidence(), _evidence()], ingestions=1),
    ],
)
def test_malformed_or_incomplete_recovery_evidence_fails_closed(result):
    assert not restored_position_feeds_ready(1, result, ["BTCUSDT"])


def test_multiple_open_trades_for_one_symbol_are_supported():
    assert restored_position_feeds_ready(2, _result(), ["BTCUSDT", "BTCUSDT"])


def test_restored_position_with_missing_symbol_blocks_startup():
    assert not restored_position_feeds_ready(1, _result(malformed=1), [None])
