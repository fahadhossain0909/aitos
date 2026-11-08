from __future__ import annotations

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
    await market.initialize({})
    await journal.initialize({})
    try:
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

        await journal.save_trade_snapshot(
            {"trade_id": trade_id, "state": "OPEN", "quantity": 0.25}
        )
        await journal.save_trade_snapshot(
            {"trade_id": trade_id, "state": "OPEN", "quantity": 0.25}
        )
        entry = JournalEntry(
            trade_id=trade_id,
            entry_type=JournalEntryType.PRE_TRADE,
            market_context={"symbol": symbol, "timeframe": "1m"},
        )
        await journal.save_journal_entry(entry)
        await journal.save_journal_entry(entry)
        await journal.save_decision(
            decision_id, {"candidate_id": token, "symbol": symbol}
        )
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
    finally:
        async with market._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM aitos_market_records WHERE entity_id = $1", symbol
            )
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
