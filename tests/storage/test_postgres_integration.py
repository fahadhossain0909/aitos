from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from aitos.data.postgres_repository import PostgresMarketDataRepository
from aitos.journal.models import JournalEntry, JournalEntryType
from aitos.journal.postgres_repository import PostgresJournalRepository
from aitos.models.market import Kline


@pytest.mark.asyncio
async def test_postgres_market_and_journal_round_trip():
    dsn = os.getenv("AITOS_TEST_POSTGRES_URL")
    if not dsn:
        pytest.skip("AITOS_TEST_POSTGRES_URL is not configured")

    token = uuid.uuid4().hex[:10].upper()
    symbol = f"IT{token}USDT"
    trade_id = f"trade-{token}"
    decision_id = f"decision-{token}"
    opened = datetime.now(timezone.utc).replace(microsecond=0)
    closed = opened + timedelta(minutes=1)

    market = PostgresMarketDataRepository(dsn)
    journal = PostgresJournalRepository(dsn)
    retry_market = PostgresMarketDataRepository(dsn)
    retry_journal = PostgresJournalRepository(dsn)
    # Independent adapter pools race through the same migration lock. The
    # schema must be created once and every process must observe the same
    # committed migration version.
    await asyncio.gather(
        market.initialize({}),
        journal.initialize({}),
        retry_market.initialize({}),
        retry_journal.initialize({}),
    )
    try:
        async with market._pool.acquire() as conn:
            migration_rows = await conn.fetch(
                "SELECT version, name, checksum FROM aitos_schema_migrations "
                "ORDER BY version"
            )
        assert [(row["version"], row["name"]) for row in migration_rows] == [
            (1, "operational_and_market_records")
        ]
        assert len(migration_rows[0]["checksum"]) == 64
        initial = Kline(
            symbol=symbol,
            timeframe="1m",
            open_time=opened,
            close_time=closed,
            open=100.0,
            high=102.0,
            low=99.0,
            close=101.0,
            volume=10.0,
            quote_volume=1000.0,
            trades_count=12,
            taker_buy_volume=6.0,
            taker_buy_quote_volume=600.0,
        )
        updated = Kline(
            symbol=symbol,
            timeframe="1m",
            open_time=opened,
            close_time=closed,
            open=100.0,
            high=103.0,
            low=99.0,
            close=102.0,
            volume=12.0,
            quote_volume=1200.0,
            trades_count=14,
            taker_buy_volume=7.0,
            taker_buy_quote_volume=700.0,
        )
        await market.save_kline(initial)
        await market.save_kline(updated)
        klines = await market.get_recent_klines(symbol, "1m", limit=10)
        assert len(klines) == 1
        assert klines[0]["close"] == 102.0
        assert isinstance(klines[0]["time"], datetime)

        snapshot = {"trade_id": trade_id, "state": "OPEN", "quantity": 0.25}
        await journal.save_trade_snapshot(snapshot)
        await journal.save_trade_snapshot(snapshot)
        entry = JournalEntry(
            trade_id=trade_id,
            entry_type=JournalEntryType.PRE_TRADE,
            market_context={"symbol": symbol, "timeframe": "1m"},
        )
        await journal.save_journal_entry(entry)
        await journal.save_journal_entry(entry)
        decision_snapshot = {"candidate_id": token, "symbol": symbol}
        # Simulate a process dying after PostgreSQL committed but before the
        # caller received its acknowledgement. A fresh adapter retry must
        # collide with the durable idempotency key rather than append twice.
        async with retry_journal._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO aitos_operational_records
                    (record_type, entity_id, idempotency_key, recorded_at, payload)
                VALUES ('DECISION', $1, $2, $3, $4::jsonb)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                decision_id,
                f"decision:{decision_id}",
                opened,
                json.dumps(decision_snapshot),
            )
        await journal.save_decision(decision_id, decision_snapshot)
        await journal.link_trade(decision_id, {"trade_id": trade_id, "symbol": symbol})
        await journal.attribute_outcome(
            decision_id,
            {
                "trade_id": trade_id,
                "pnl": 1.5,
                "risk_amount_usd": 0.5,
                "entry_time": opened.isoformat(),
                "exit_time": closed.isoformat(),
            },
        )

        # Concurrent retries through independent pools must remain idempotent.
        await asyncio.gather(
            *(retry_market.save_kline(updated) for _ in range(12)),
            *(retry_journal.save_trade_snapshot(snapshot) for _ in range(12)),
            *(retry_journal.save_journal_entry(entry) for _ in range(12)),
            *(
                retry_journal.save_decision(
                    decision_id, {"candidate_id": token, "symbol": symbol}
                )
                for _ in range(12)
            ),
        )

        journal_entries = await journal.get_journal_entries_for_trade(trade_id)
        decision_records = await journal.get_records(decision_id)
        trade_records = await journal.get_trade_records(trade_id)
        assert len(journal_entries) == 1
        assert {row["record_type"] for row in decision_records} >= {
            "DECISION",
            "TRADE_LINK",
            "OUTCOME",
        }
        outcome = next(
            row for row in decision_records if row["record_type"] == "OUTCOME"
        )
        assert outcome["r_multiple"] == 3.0
        assert outcome["holding_seconds"] == 60.0
        assert any(row["record_type"] == "TRADE_LINK" for row in trade_records)

        # Simulate process restart: close every pool, construct fresh adapters,
        # and verify persisted state and idempotency survive reconnection.
        await market.shutdown()
        await journal.shutdown()
        await retry_market.shutdown()
        await retry_journal.shutdown()

        market = PostgresMarketDataRepository(dsn)
        journal = PostgresJournalRepository(dsn)
        await market.initialize({})
        await journal.initialize({})
        persisted_klines = await market.get_recent_klines(symbol, "1m", limit=10)
        persisted_entries = await journal.get_journal_entries_for_trade(trade_id)
        persisted_decisions = await journal.get_records(decision_id)
        assert len(persisted_klines) == 1
        assert persisted_klines[0]["close"] == 102.0
        assert len(persisted_entries) == 1
        assert {row["record_type"] for row in persisted_decisions} >= {
            "DECISION",
            "TRADE_LINK",
            "OUTCOME",
        }
        await journal.save_journal_entry(entry)
        await journal.save_decision(decision_id, decision_snapshot)
        assert len(await journal.get_journal_entries_for_trade(trade_id)) == 1
        recovered_decisions = await journal.get_records(decision_id)
        assert sum(row["record_type"] == "DECISION" for row in recovered_decisions) == 1

        # A database created by a newer application build must fail closed.
        async with journal._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO aitos_schema_migrations (version, name, checksum)
                VALUES (999, 'future_migration', repeat('0', 64))
                """
            )
        future_probe = PostgresJournalRepository(dsn)
        try:
            with pytest.raises(RuntimeError, match="newer than this AITOS build"):
                await future_probe.initialize({})
        finally:
            await future_probe.shutdown()
            async with journal._pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM aitos_schema_migrations WHERE version = 999"
                )
    finally:
        if market._pool is not None:
            async with market._pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM aitos_market_records WHERE entity_id = $1", symbol
                )
        if journal._pool is not None:
            async with journal._pool.acquire() as conn:
                await conn.execute(
                    """
                    DELETE FROM aitos_operational_records
                    WHERE entity_id IN ($1, $2) OR payload->>'trade_id' = $1
                    """,
                    trade_id,
                    decision_id,
                )
        await market.shutdown()
        await journal.shutdown()
        await retry_market.shutdown()
        await retry_journal.shutdown()
