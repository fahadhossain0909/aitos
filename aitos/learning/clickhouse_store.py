"""ClickHouse persistence for long-lived learning experiences.

The store is intentionally append-only: historical experience is valuable for
future replay, training, evaluation, and error analysis and must not be treated
as a transient cache.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .experience import ExperienceRecord

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
        self.client.command(CREATE_EXPERIENCES_SQL.format(database=self.database))

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
        return len(rows)

    def query_window(
        self, symbol: str, start: datetime, end: datetime, limit: int = 1_000_000
    ):
        return self.client.query(
            f"SELECT * FROM {self.database}.learning_experiences "  # nosec B608 - database is trusted configuration, values are parameterized
            "WHERE symbol = {symbol:String} AND timestamp >= {start:DateTime64(3)} "
            "AND timestamp < {end:DateTime64(3)} ORDER BY timestamp LIMIT {limit:UInt32}",
            parameters={"symbol": symbol, "start": start, "end": end, "limit": limit},
        ).result_rows


def _json(value: dict) -> str:
    import json

    return json.dumps(value, sort_keys=True, default=str)
