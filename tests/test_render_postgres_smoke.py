from __future__ import annotations

import asyncio

import pytest

from aitos.data import postgres_repository as market_module
from aitos.journal import postgres_repository as journal_module
from scripts import render_postgres_smoke as smoke


class _Connection:
    async def fetchval(self, query):
        assert "aitos_schema_migrations" in query
        return 1


class _Acquire:
    async def __aenter__(self):
        return _Connection()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Pool:
    def acquire(self):
        return _Acquire()


class _Market:
    def __init__(self, dsn, **kwargs):
        self._pool = _Pool()
        self.closed = False

    async def initialize(self, config):
        return None

    async def get_recent_klines(self, symbol, timeframe, limit):
        assert (symbol, timeframe, limit) == (
            smoke.SMOKE_SYMBOL,
            smoke.SMOKE_TIMEFRAME,
            5,
        )
        return [{"close": smoke.SMOKE_CLOSE, "time": smoke.SMOKE_OPENED}]

    async def shutdown(self):
        self.closed = True

    def __getattr__(self, name):
        if name.startswith("save_"):
            raise AssertionError("health verification must never write")
        raise AttributeError(name)


class _Journal:
    def __init__(self, dsn, **kwargs):
        self.closed = False

    async def initialize(self, config):
        return None

    async def get_journal_entries_for_trade(self, trade_id):
        assert trade_id == smoke.SMOKE_TRADE_ID
        return [{"entry_id": smoke.SMOKE_ENTRY_ID}]

    async def get_records(self, decision_id):
        assert decision_id == smoke.SMOKE_DECISION_ID
        return [
            {"record_type": "DECISION"},
            {"record_type": "TRADE_LINK"},
            {"record_type": "OUTCOME"},
        ]

    async def get_trade_records(self, trade_id):
        assert trade_id == smoke.SMOKE_TRADE_ID
        return [
            {
                "record_type": "TRADE_SNAPSHOT",
                "trade_id": smoke.SMOKE_TRADE_ID,
                "state": "OPEN",
            },
            {"record_type": "JOURNAL_ENTRY"},
            {"record_type": "TRADE_LINK"},
        ]

    async def shutdown(self):
        self.closed = True

    def __getattr__(self, name):
        if name.startswith("save_") or name in {"link_trade", "attribute_outcome"}:
            raise AssertionError("health verification must never write")
        raise AttributeError(name)


def test_hosted_health_probe_reads_existing_records_without_writes(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(market_module, "PostgresMarketDataRepository", _Market)
    monkeypatch.setattr(journal_module, "PostgresJournalRepository", _Journal)

    result = asyncio.run(smoke._verify_persisted())

    assert result == {
        "check": "aitos_postgres_persistence_read_only",
        "status": "ok",
        "schema_version": 1,
        "market_data": "persisted",
        "operational_journal": "persisted",
        "write_performed": False,
    }


def test_hosted_health_probe_fails_closed_when_market_sentinel_is_missing(
    monkeypatch,
):
    class MissingMarket(_Market):
        async def get_recent_klines(self, symbol, timeframe, limit):
            return []

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(market_module, "PostgresMarketDataRepository", MissingMarket)
    monkeypatch.setattr(journal_module, "PostgresJournalRepository", _Journal)

    with pytest.raises(RuntimeError, match="market-data sentinel is missing"):
        asyncio.run(smoke._verify_persisted())
