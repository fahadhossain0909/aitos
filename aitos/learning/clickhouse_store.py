"""ClickHouse persistence for long-lived learning experiences.

The store is intentionally append-only: historical experience is valuable for
future replay, training, evaluation, and error analysis and must not be treated
as a transient cache.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from aitos.logging_setup import get_logger

from .experience import ExperienceRecord

logger = get_logger("aitos.learning.clickhouse_store")

CREATE_EXPERIENCES_SQL = """
CREATE TABLE IF NOT EXISTS {database}.learning_experiences
(
    experience_id UUID,
    timestamp DateTime64(3, 'UTC'),
    source LowCardinality(String),
    symbol LowCardinality(String),
    decision LowCardinality(String),
    outcome Nullable(String),
    reward Float64,
    confidence Float64,
    quantity Float64,
    price Nullable(Float64),
    features_json String,
    market_state_json String,
    risk_state_json String,
    strategy_version String,
    model_version String,
    metadata_json String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (symbol, timestamp, experience_id)
"""


class ClickHouseExperienceStore:
    """Small adapter used by all execution stages to persist experiences."""

    def __init__(self, client, database: str = "aitos") -> None:
        self.client = client
        self.database = database

    def ensure_schema(self) -> None:
        try:
            self.client.command(CREATE_EXPERIENCES_SQL.format(database=self.database))
            logger.info(
                "learning experience schema ensured",
                extra={"aitos_extra": {"database": self.database}},
            )
        except Exception:
            logger.exception(
                "learning experience schema ensure failed",
                extra={"aitos_extra": {"database": self.database}},
            )
            raise

    def append(self, records: Iterable[ExperienceRecord]) -> int:
        rows = []
        for record in records:
            rows.append(
                (
                    record.experience_id,
                    record.timestamp,
                    record.source,
                    record.symbol,
                    record.decision,
                    record.outcome,
                    record.reward,
                    record.confidence,
                    record.quantity,
                    record.price,
                    _json(record.features),
                    _json(record.market_state),
                    _json(record.risk_state),
                    record.strategy_version,
                    record.model_version,
                    _json(record.metadata),
                )
            )
        if not rows:
            return 0
        try:
            self.client.insert(
                f"{self.database}.learning_experiences",
                rows,
                column_names=[
                    "experience_id",
                    "timestamp",
                    "source",
                    "symbol",
                    "decision",
                    "outcome",
                    "reward",
                    "confidence",
                    "quantity",
                    "price",
                    "features_json",
                    "market_state_json",
                    "risk_state_json",
                    "strategy_version",
                    "model_version",
                    "metadata_json",
                ],
            )
        except Exception:
            logger.exception(
                "learning experience append failed",
                extra={
                    "aitos_extra": {
                        "database": self.database,
                        "rows": len(rows),
                    }
                },
            )
            raise
        logger.info(
            "learning experiences appended",
            extra={
                "aitos_extra": {
                    "database": self.database,
                    "rows": len(rows),
                }
            },
        )
        return len(rows)

    def query_window(
        self, symbol: str, start: datetime, end: datetime, limit: int = 1_000_000
    ):
        try:
            rows = self.client.query(
                f"SELECT * FROM {self.database}.learning_experiences "  # nosec B608 - database is trusted configuration, values are parameterized
                "WHERE symbol = {symbol:String} AND timestamp >= {start:DateTime64(3)} "
                "AND timestamp < {end:DateTime64(3)} ORDER BY timestamp LIMIT {limit:UInt32}",
                parameters={
                    "symbol": symbol,
                    "start": start,
                    "end": end,
                    "limit": limit,
                },
            ).result_rows
        except Exception:
            logger.exception(
                "learning experience query_window failed",
                extra={
                    "aitos_extra": {
                        "symbol": symbol,
                        "start": start.isoformat() if start else None,
                        "end": end.isoformat() if end else None,
                        "limit": limit,
                    }
                },
            )
            raise
        if not rows:
            logger.warning(
                "learning experience query_window returned no rows",
                extra={"aitos_extra": {"symbol": symbol, "limit": limit}},
            )
        return rows


def _json(value: dict) -> str:
    import json

    return json.dumps(value, sort_keys=True, default=str)
