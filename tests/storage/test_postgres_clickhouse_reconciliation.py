from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from aitos.data.postgres_repository import PostgresMarketDataRepository
from aitos.data.repository import MarketDataRepository
from aitos.journal.decision_repository import DecisionJournalRepository
from aitos.journal.models import JournalEntry, JournalEntryType
from aitos.journal.postgres_repository import PostgresJournalRepository
from aitos.journal.repository import JournalRepository
from aitos.market_data.contracts import MarketEvent, MarketEventType, MarketSource
from aitos.models.market import (
    FundingRate,
    Kline,
    OpenInterest,
    OrderBookSnapshot,
    TradeSide,
    TradeTick,
)
from aitos.storage.postgres_migrations import apply_postgres_migrations
from scripts.reconcile_postgres_journal import reconcile


@pytest.mark.asyncio
async def test_postgres_fallback_market_and_operational_records_reconcile_with_clickhouse():
    postgres_url = os.getenv("AITOS_TEST_POSTGRES_URL")
    clickhouse_host = os.getenv("CLICKHOUSE_HOST")
    if not postgres_url or not clickhouse_host:
        pytest.skip("PostgreSQL and ClickHouse integration services are required")

    token = uuid.uuid4().hex[:10].upper()
    symbol = f"RC{token}USDT"
    trade_id = f"reconcile-trade-{token}"
    decision_id = f"reconcile-decision-{token}"
    entry_id = f"reconcile-entry-{token}"
    now = datetime.now(timezone.utc).replace(microsecond=123000)

    pg_market = PostgresMarketDataRepository(postgres_url)
    pg_journal = PostgresJournalRepository(postgres_url)
    ch_market = MarketDataRepository(
        host=clickhouse_host,
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_DATABASE", "aitos_test"),
    )
    ch_journal = JournalRepository(
        host=clickhouse_host,
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_DATABASE", "aitos_test"),
    )
    ch_decisions = DecisionJournalRepository(
        host=clickhouse_host,
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_DATABASE", "aitos_test"),
    )

    await pg_market.initialize({})
    await pg_journal.initialize({})
    await ch_market.initialize({})
    await ch_journal.initialize({})
    await ch_decisions.initialize({})
    try:
        kline = Kline(
            symbol=symbol,
            timeframe="1m",
            open_time=now,
            close_time=now + timedelta(minutes=1),
            open=100.0,
            high=102.0,
            low=99.0,
            close=101.0,
            volume=12.0,
            quote_volume=1212.0,
            trades_count=17,
            taker_buy_volume=7.0,
            taker_buy_quote_volume=707.0,
        )
        book = OrderBookSnapshot(
            symbol=symbol,
            bids=((100.0, 2.0), (99.0, 3.0)),
            asks=((101.0, 2.0), (102.0, 3.0)),
            last_update_id=123456,
            timestamp=now,
            depth_event={
                "event_time_ms": int(now.timestamp() * 1000),
                "final_update_id": 123456,
                "previous_update_id": 123455,
                "bids": [["100.0", "2.0"]],
                "asks": [["101.0", "2.0"]],
            },
        )
        tick = TradeTick(
            symbol=symbol,
            trade_id=987654,
            price=100.5,
            quantity=0.75,
            side=TradeSide.BUY,
            is_buyer_maker=False,
            timestamp=now,
        )
        funding = FundingRate(
            symbol=symbol,
            funding_rate=0.0001,
            funding_time=now,
            mark_price=100.5,
        )
        open_interest = OpenInterest(
            symbol=symbol,
            open_interest=12345.0,
            timestamp=now,
        )
        market_event = MarketEvent(
            event_type=MarketEventType.TRADE,
            exchange="binance",
            market="futures",
            symbol=symbol,
            event_time=now,
            ingest_time=now,
            payload={"trade_id": 987654, "price": 100.5, "quantity": 0.75},
            source=MarketSource.WEBSOCKET,
            event_id=f"reconcile-event-{token}",
            schema_version=1,
            sequence=987654,
        )
        experience_id = str(uuid.uuid4())
        experience = type(
            "Experience",
            (),
            {
                "experience_id": experience_id,
                "timestamp": now,
                "source": "paper",
                "symbol": symbol,
                "decision": "LONG",
                "outcome": "WIN",
                "reward": 1.25,
                "confidence": 0.82,
                "quantity": 0.75,
                "price": 100.5,
                "features": {"imbalance": 0.4},
                "market_state": {"regime": "range"},
                "risk_state": {"risk_usd": 0.25},
                "strategy_version": "reconcile-test",
                "model_version": "reconcile-test",
                "metadata": {"fixture": token},
            },
        )()

        for repository in (pg_market, ch_market):
            await repository.save_kline(kline)
            await repository.save_order_book_snapshot(book)
            await repository.save_trade_tick(tick)
            await repository.save_funding_rate(funding)
            await repository.save_open_interest(open_interest)
            await repository.save_market_event(market_event)
            await repository.save_live_analytics_event(
                category="RECONCILIATION_TEST",
                symbol=symbol,
                payload={"fixture": token, "imbalance": 0.4},
                event_time=now,
                ingest_time=now,
                event_id=f"reconcile-live-{token}",
                exchange="binance",
                market="futures",
                source_module="storage-integration-test",
                correlation_id=f"reconcile-correlation-{token}",
                schema_version="1.0",
            )
            await repository.save_learning_experience(experience)

        trade_snapshot = {
            "trade_id": trade_id,
            "symbol": symbol,
            "side": "LONG",
            "entry_price": 100.0,
            "quantity": 0.75,
            "leverage": 1.0,
            "position_size_usd": 75.0,
            "risk_amount_usd": 0.5,
            "strategy_id": "reconcile-test",
            "sl_price": 99.0,
            "tp_price": 102.0,
            "state": "OPEN",
            "entry_time": now.isoformat(),
        }
        journal_entry = JournalEntry(
            entry_id=entry_id,
            trade_id=trade_id,
            entry_type=JournalEntryType.PRE_TRADE,
            market_context={"symbol": symbol, "fixture": token},
            confidence_score=0.82,
            order_flow_observations={"imbalance": 0.4},
            created_at=now.isoformat(),
        )
        decision_snapshot = {
            "candidate_id": token,
            "symbol": symbol,
            "side": "LONG",
            "strategy_id": "reconcile-test",
            "regime": "range",
            "confidence": 0.82,
        }
        trade_link = {
            "trade_id": trade_id,
            "symbol": symbol,
            "side": "LONG",
            "strategy_id": "reconcile-test",
            "regime": "range",
        }
        outcome = {
            "trade_id": trade_id,
            "symbol": symbol,
            "side": "LONG",
            "strategy_id": "reconcile-test",
            "regime": "range",
            "pnl": 1.5,
            "pnl_percent": 2.0,
            "risk_amount_usd": 0.5,
            "entry_time": now.isoformat(),
            "exit_time": (now + timedelta(minutes=2)).isoformat(),
            "exit_reason": "target",
        }

        await pg_journal.save_trade_snapshot(trade_snapshot)
        await ch_journal.save_trade_snapshot(trade_snapshot)
        await pg_journal.save_journal_entry(journal_entry)
        await ch_journal.save_journal_entry(journal_entry)
        await pg_journal.save_decision(decision_id, decision_snapshot)
        await ch_decisions.save_decision(decision_id, decision_snapshot)
        await pg_journal.link_trade(decision_id, trade_link)
        await ch_decisions.link_trade(decision_id, trade_link)
        await pg_journal.attribute_outcome(decision_id, outcome)
        await ch_decisions.attribute_outcome(decision_id, outcome)

        report = await reconcile(now - timedelta(minutes=5))

        assert report["clean"], json_report(report)
        assert all(result["clean"] for result in report["market_data"].values())
        assert all(result["clean"] for result in report["operational"].values())
    finally:
        await pg_market.shutdown()
        await pg_journal.shutdown()
        await ch_market.shutdown()
        await ch_journal.shutdown()
        await ch_decisions.shutdown()


def json_report(report):
    import json

    return json.dumps(report, indent=2, sort_keys=True)
