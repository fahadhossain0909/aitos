from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aitos.intelligence.scanner import OpportunityScanner


def _scanner_with_state(state):
    class FakeCache:
        def snapshot(self, symbol):
            return state

    scanner = object.__new__(OpportunityScanner)
    scanner._live_cache = FakeCache()
    scanner._live_state_stale_seconds = 5.0
    return scanner


def test_missing_live_state_reports_both_feeds_stale():
    scanner = _scanner_with_state(None)
    trades, book, trade_fresh, book_fresh = scanner._live_market_data("BTCUSDT")
    assert trades == []
    assert book is None
    assert trade_fresh is False
    assert book_fresh is False


def test_fresh_live_trades_are_preserved_when_order_book_is_missing():
    now = datetime.now(timezone.utc)
    state = SimpleNamespace(
        trades=[object()],
        order_book=None,
        last_trade_at=now,
        last_book_at=None,
    )
    scanner = _scanner_with_state(state)

    trades, book, trade_fresh, book_fresh = scanner._live_market_data("BTCUSDT")

    assert len(trades) == 1
    assert book is None
    assert trade_fresh is True
    assert book_fresh is False


def test_trade_and_book_freshness_are_evaluated_independently():
    now = datetime.now(timezone.utc)
    state = SimpleNamespace(
        trades=[object()],
        order_book=object(),
        last_trade_at=now - timedelta(seconds=10),
        last_book_at=now,
    )
    scanner = _scanner_with_state(state)

    trades, book, trade_fresh, book_fresh = scanner._live_market_data("BTCUSDT")

    assert trades == []
    assert book is state.order_book
    assert trade_fresh is False
    assert book_fresh is True


def test_scanner_version_reflects_live_state_primary_path():
    assert OpportunityScanner.version.fget(None) == "1.10.1"
