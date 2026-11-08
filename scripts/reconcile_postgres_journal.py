"""Read-only ClickHouse/PostgreSQL operational journal reconciliation.

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
from typing import Any

from aitos.storage.reconciliation import compare_records


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {"value": decoded}
    return dict(value)


async def _fetch_clickhouse_rows(client, query: str, since: datetime) -> list[dict[str, Any]]:
    result = await client.query(query, parameters={"since": since})
    return [dict(zip(result.column_names, row)) for row in result.result_rows]


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
            record = {
                "record_type": record_type,
                "entity_id": row["entity_id"],
                "payload": payload,
            }
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
            {
                "record_type": "TRADE_SNAPSHOT",
                "entity_id": str(row["trade_id"]),
                "payload": _payload(row["payload"]),
            }
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
                {
                    "record_type": "JOURNAL_ENTRY",
                    "entity_id": str(row["trade_id"] or row["entry_id"]),
                    "payload": payload,
                }
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
            {
                "record_type": row["record_type"],
                "entity_id": str(row["decision_id"]),
                "payload": _payload(row["payload"]),
            }
            for row in ch_decision_rows
        ]

        results = {
            "trade_snapshots": compare_records(
                pg_trade_snapshots,
                ch_trade_snapshots,
                excluded_payload_fields=frozenset({"recorded_at"}),
            ),
            "journal_entries": compare_records(
                pg_journal_entries, ch_journal_entries
            ),
            "decision_lifecycle": compare_records(
                pg_decisions,
                ch_decisions,
                excluded_payload_fields=frozenset({"r_multiple", "holding_seconds"}),
            ),
        }
        return {
            "mode": "read_only",
            "since": since.isoformat(),
            "source": "postgresql",
            "target": "clickhouse",
            "domains": {name: result.to_dict() for name, result in results.items()},
            "clean": all(result.clean for result in results.values()),
            "limitations": [
                "This audit covers operational trade snapshots, journal entries, and decision lifecycle records only.",
                "Market OHLCV, depth, ticks, funding, learning experiences, and analytical feature tables are not reconciled by this command.",
                "A clean comparison is not permission to switch backends; operator approval and a separately tested migration are still required.",
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
