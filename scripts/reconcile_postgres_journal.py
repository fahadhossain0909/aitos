"""Read-only ClickHouse/PostgreSQL operational and market-data reconciliation.

The command compares a bounded time window in both directions. It never copies,
deletes, or rewrites records. Any difference requires an operator-reviewed
migration plan and a separately approved write operation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from aitos.storage.reconciliation import compare_records


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {"value": decoded}
    return dict(value)


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def _fetch_clickhouse_rows(
    client, query: str, since: datetime
) -> list[dict[str, Any]]:
    result = await client.query(query, parameters={"since": since})
    return [dict(zip(result.column_names, row)) for row in result.result_rows]


def _record(record_type: str, entity_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_type": record_type,
        "entity_id": str(entity_id),
        "payload": payload,
    }


async def _reconcile_operational(pg_pool, ch, since: datetime) -> dict[str, Any]:
    async with pg_pool.acquire() as conn:
        pg_rows = await conn.fetch(
            """
            SELECT record_type, entity_id, payload::text AS payload
            FROM aitos_operational_records
            WHERE recorded_at >= $1
            ORDER BY recorded_at, record_id
            """,
            since,
        )

    pg_trade_snapshots: list[dict[str, Any]] = []
    pg_journal_entries: list[dict[str, Any]] = []
    pg_decisions: list[dict[str, Any]] = []
    for row in pg_rows:
        record_type = row["record_type"]
        payload = _payload(row["payload"])
        record = _record(record_type, row["entity_id"], payload)
        if record_type == "TRADE_SNAPSHOT":
            pg_trade_snapshots.append(record)
        elif record_type == "JOURNAL_ENTRY":
            pg_journal_entries.append(record)
        elif record_type in {
            "DECISION",
            "TRADE_LINK",
            "OUTCOME",
            "MANAGEMENT_DECISION",
        }:
            pg_decisions.append(record)

    ch_trade_rows = await _fetch_clickhouse_rows(
        ch,
        """
        SELECT trade_id, payload
        FROM trades
        WHERE recorded_at >= {since:DateTime64(3, 'UTC')}
        ORDER BY recorded_at, trade_id
        """,
        since,
    )
    ch_trade_snapshots = [
        _record("TRADE_SNAPSHOT", row["trade_id"], _payload(row["payload"]))
        for row in ch_trade_rows
    ]

    ch_journal_rows = await _fetch_clickhouse_rows(
        ch,
        """
        SELECT entry_id, trade_id, entry_type, market_context,
               confidence_score, order_flow_observations,
               liquidity_observations, amt_observations,
               lead_lag_observations, mistakes, lessons, improvements
        FROM journal_entries
        WHERE created_at >= {since:DateTime64(3, 'UTC')}
        ORDER BY created_at, entry_id
        """,
        since,
    )
    ch_journal_entries = []
    for row in ch_journal_rows:
        payload = {
            "entry_id": row["entry_id"],
            "trade_id": row["trade_id"],
            "entry_type": row["entry_type"],
            "market_context": _payload(row["market_context"]),
            "confidence_score": row["confidence_score"],
            "order_flow_observations": _payload(row["order_flow_observations"]),
            "liquidity_observations": _payload(row["liquidity_observations"]),
            "amt_observations": _payload(row["amt_observations"]),
            "lead_lag_observations": _payload(row["lead_lag_observations"]),
            "mistakes": list(row["mistakes"]),
            "lessons": list(row["lessons"]),
            "improvements": list(row["improvements"]),
        }
        ch_journal_entries.append(
            _record("JOURNAL_ENTRY", row["trade_id"] or row["entry_id"], payload)
        )

    ch_decision_rows = await _fetch_clickhouse_rows(
        ch,
        """
        SELECT decision_id, record_type, payload
        FROM decision_journal
        WHERE recorded_at >= {since:DateTime64(3, 'UTC')}
        ORDER BY recorded_at, decision_id
        """,
        since,
    )
    ch_decisions = [
        _record(row["record_type"], row["decision_id"], _payload(row["payload"]))
        for row in ch_decision_rows
    ]

    return {
        "trade_snapshots": compare_records(
            pg_trade_snapshots,
            ch_trade_snapshots,
            excluded_payload_fields=frozenset({"recorded_at"}),
        ).to_dict(),
        "journal_entries": compare_records(
            pg_journal_entries, ch_journal_entries
        ).to_dict(),
        "decision_lifecycle": compare_records(
            pg_decisions,
            ch_decisions,
            excluded_payload_fields=frozenset({"r_multiple", "holding_seconds"}),
        ).to_dict(),
    }


async def _reconcile_market_data(pg_pool, ch, since: datetime) -> dict[str, Any]:
    async with pg_pool.acquire() as conn:
        pg_rows = await conn.fetch(
            """
            SELECT record_type, entity_id, idempotency_key, event_time,
                   payload::text AS payload
            FROM aitos_market_records
            WHERE event_time >= $1
            ORDER BY event_time, record_id
            """,
            since,
        )

    pg_records: dict[str, list[dict[str, Any]]] = {}
    for row in pg_rows:
        record_type = row["record_type"]
        payload = _payload(row["payload"])
        if record_type == "LIVE_ANALYTICS":
            payload["event_time"] = row["event_time"]
            payload["event_id"] = row["idempotency_key"].removeprefix(
                "live-analytics:"
            )
        elif record_type == "DEPTH_EVENT":
            payload["event_time"] = row["event_time"]
        pg_records.setdefault(record_type, []).append(
            _record(record_type, row["entity_id"], payload)
        )

    queries = {
        "KLINE": """
            SELECT time, symbol, timeframe, open, high, low, close, volume,
                   quote_volume, trades_count, taker_buy_volume,
                   taker_buy_quote_volume
            FROM market_ohlcv
            WHERE time >= {since:DateTime64(3, 'UTC')}
            ORDER BY time, symbol, timeframe
        """,
        "MARKET_EVENT": """
            SELECT event_time, ingest_time, event_id, exchange, market, symbol,
                   event_type, source, sequence, schema_version, correlation_id,
                   trace_id, payload_json
            FROM market_events
            WHERE event_time >= {since:DateTime64(3, 'UTC')}
            ORDER BY event_time, symbol, event_id
        """,
        "LIVE_ANALYTICS": """
            SELECT event_time, ingest_time, event_id, category, exchange, market,
                   symbol, source_module, correlation_id, schema_version,
                   payload_json
            FROM live_analytics_events
            WHERE event_time >= {since:DateTime64(3, 'UTC')}
            ORDER BY event_time, category, symbol, event_id
        """,
        "ORDER_BOOK_SNAPSHOT": """
            SELECT time, symbol, bid_levels, ask_levels, spread, depth_ratio,
                   last_update_id
            FROM order_book_snapshots
            WHERE time >= {since:DateTime64(3, 'UTC')}
            ORDER BY time, symbol, last_update_id
        """,
        "DEPTH_EVENT": """
            SELECT event_time, symbol, final_update_id, previous_update_id,
                   payload_json
            FROM depth_events
            WHERE event_time >= {since:DateTime64(3, 'UTC')}
            ORDER BY event_time, symbol, final_update_id
        """,
        "TRADE_TICK": """
            SELECT time, symbol, trade_id, price, quantity, side, is_buyer_maker
            FROM trade_ticks
            WHERE time >= {since:DateTime64(3, 'UTC')}
            ORDER BY time, symbol, trade_id
        """,
        "FUNDING_RATE": """
            SELECT time, symbol, funding_rate, mark_price
            FROM funding_rates
            WHERE time >= {since:DateTime64(3, 'UTC')}
            ORDER BY time, symbol
        """,
        "OPEN_INTEREST": """
            SELECT time, symbol, open_interest
            FROM open_interest
            WHERE time >= {since:DateTime64(3, 'UTC')}
            ORDER BY time, symbol
        """,
        "LEARNING_EXPERIENCE": """
            SELECT experience_id, timestamp, source, symbol, decision, outcome,
                   reward, confidence, quantity, price, features_json,
                   market_state_json, risk_state_json, strategy_version,
                   model_version, metadata_json
            FROM learning_experiences
            WHERE timestamp >= {since:DateTime64(3, 'UTC')}
            ORDER BY timestamp, symbol, experience_id
        """,
    }

    ch_records: dict[str, list[dict[str, Any]]] = {}
    for record_type, query in queries.items():
        rows = await _fetch_clickhouse_rows(ch, query, since)
        normalized = []
        for row in rows:
            if record_type == "KLINE":
                payload = dict(row)
                payload["time"] = row["time"]
                entity_id = row["symbol"]
            elif record_type == "MARKET_EVENT":
                payload = {
                    "event_time": row["event_time"],
                    "ingest_time": row["ingest_time"],
                    "event_id": row["event_id"],
                    "exchange": row["exchange"],
                    "market": row["market"],
                    "symbol": row["symbol"],
                    "event_type": row["event_type"],
                    "source": row["source"],
                    "sequence": row["sequence"],
                    "schema_version": row["schema_version"],
                    "correlation_id": row["correlation_id"],
                    "trace_id": row["trace_id"],
                    "payload": _payload(row["payload_json"]),
                }
                entity_id = row["symbol"]
            elif record_type == "LIVE_ANALYTICS":
                payload = {
                    "event_time": row["event_time"],
                    "ingest_time": row["ingest_time"],
                    "event_id": row["event_id"],
                    "category": row["category"],
                    "exchange": row["exchange"],
                    "market": row["market"],
                    "symbol": row["symbol"],
                    "source_module": row["source_module"],
                    "correlation_id": row["correlation_id"],
                    "schema_version": row["schema_version"],
                    "payload": _payload(row["payload_json"]),
                }
                entity_id = row["symbol"]
            elif record_type == "ORDER_BOOK_SNAPSHOT":
                payload = {
                    "timestamp": row["time"],
                    "symbol": row["symbol"],
                    "bids": _json_value(row["bid_levels"]),
                    "asks": _json_value(row["ask_levels"]),
                    "spread": row["spread"],
                    "depth_ratio": row["depth_ratio"],
                    "last_update_id": row["last_update_id"],
                }
                entity_id = row["symbol"]
            elif record_type == "DEPTH_EVENT":
                payload = {
                    "event_time": row["event_time"],
                    "symbol": row["symbol"],
                    "final_update_id": row["final_update_id"],
                    "previous_update_id": row["previous_update_id"],
                    "payload": _payload(row["payload_json"]),
                }
                entity_id = row["symbol"]
            elif record_type == "TRADE_TICK":
                payload = dict(row)
                payload["time"] = row["time"]
                payload["is_buyer_maker"] = bool(row["is_buyer_maker"])
                entity_id = row["symbol"]
            elif record_type == "FUNDING_RATE":
                payload = dict(row)
                payload["time"] = row["time"]
                entity_id = row["symbol"]
            elif record_type == "OPEN_INTEREST":
                payload = dict(row)
                payload["time"] = row["time"]
                entity_id = row["symbol"]
            else:
                payload = {
                    "experience_id": str(row["experience_id"]),
                    "timestamp": row["timestamp"],
                    "source": row["source"],
                    "symbol": row["symbol"],
                    "decision": row["decision"],
                    "outcome": row["outcome"],
                    "reward": row["reward"],
                    "confidence": row["confidence"],
                    "quantity": row["quantity"],
                    "price": row["price"],
                    "features": _payload(row["features_json"]),
                    "market_state": _payload(row["market_state_json"]),
                    "risk_state": _payload(row["risk_state_json"]),
                    "strategy_version": row["strategy_version"],
                    "model_version": row["model_version"],
                    "metadata": _payload(row["metadata_json"]),
                }
                entity_id = row["symbol"]
            normalized.append(_record(record_type, entity_id, payload))
        ch_records[record_type] = normalized

    excluded = {
        "ORDER_BOOK_SNAPSHOT": frozenset({"depth_event"}),
    }
    results = {}
    for record_type in queries:
        results[record_type.lower()] = compare_records(
            pg_records.get(record_type, []),
            ch_records.get(record_type, []),
            excluded_payload_fields=excluded.get(record_type, frozenset()),
        ).to_dict()
    return results


async def reconcile(since: datetime) -> dict[str, Any]:
    import asyncpg
    import clickhouse_connect

    postgres_url = os.getenv("AITOS_RECONCILE_POSTGRES_URL") or os.getenv(
        "AITOS_STORAGE_POSTGRES_URL"
    )
    if not postgres_url:
        raise RuntimeError(
            "Set AITOS_RECONCILE_POSTGRES_URL or AITOS_STORAGE_POSTGRES_URL"
        )

    pg_pool = await asyncpg.create_pool(dsn=postgres_url, min_size=1, max_size=3)
    ch = await clickhouse_connect.get_async_client(
        host=os.getenv("CLICKHOUSE_HOST", "localhost"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_DATABASE", "aitos"),
    )
    try:
        operational = await _reconcile_operational(pg_pool, ch, since)
        market_data = await _reconcile_market_data(pg_pool, ch, since)
        return {
            "mode": "read_only",
            "since": since.isoformat(),
            "source": "postgresql",
            "target": "clickhouse",
            "operational": operational,
            "market_data": market_data,
            "clean": all(
                result["clean"]
                for result in [*operational.values(), *market_data.values()]
            ),
            "limitations": [
                "This audit compares operational trade snapshots, journal entries, decision lifecycle records, and the PostgreSQL market-record types implemented by the paper fallback adapter.",
                "It does not reconcile trade_runtime_state, portfolio_drawdown_state, exchange execution acknowledgements, or ClickHouse-only analytical/feature tables.",
                "The audit is read-only. A clean report is not permission to switch backends; operator approval and a separately tested migration are still required.",
            ],
        }
    finally:
        await pg_pool.close()
        await ch.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        help="UTC ISO-8601 lower bound; defaults to the last seven days",
    )
    parser.add_argument(
        "--output",
        help="Optional JSON report path; stdout is used when omitted",
    )
    args = parser.parse_args()
    since = (
        datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        if args.since
        else datetime.now(timezone.utc) - timedelta(days=7)
    )
    if since.tzinfo is None:
        parser.error("--since must include a timezone")
    since = since.astimezone(timezone.utc)

    try:
        report = asyncio.run(reconcile(since))
    except Exception as exc:
        print(
            json.dumps(
                {"status": "error", "error_type": type(exc).__name__},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1

    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
    else:
        print(encoded)
    return 0 if report["clean"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
