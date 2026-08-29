from aitos.intelligence.scanner import OpportunityScanner


def test_live_market_data_requires_both_trade_and_book_freshness(monkeypatch):
    class FakeCache:
        def snapshot(self, symbol):
            return None

    scanner = object.__new__(OpportunityScanner)
    scanner._live_cache = FakeCache()
    scanner._live_state_stale_seconds = 5.0
    trades, book, fresh = scanner._live_market_data("BTCUSDT")
    assert trades == []
    assert book is None
    assert fresh is False


def test_scanner_version_reflects_live_state_primary_path():
    assert OpportunityScanner.version.fget(None) == "1.9.0"
