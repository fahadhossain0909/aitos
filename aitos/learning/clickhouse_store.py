"""ClickHouse persistence for long-lived learning experiences.

The store is intentionally append-only: historical experience is valuable for
future replay, training, evaluation, and error analysis and must not be treated
as a transient cache.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from aitos.logging_setup import get_logger

from .experience import ExperienceRecord

logger = get_logger("aitos.learning.clickhouse_store")

CREATE_EXPERIENCES_SQL = """
CREATE TABLE IF NOT EXISTS {database}.learning_experiences
(
    experience_id UUID,
    timestamp DateTime64(3, 'UTC'),
    ingested_at DateTime64(3, 'UTC') DEFAULT timestamp,
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
            self.client.command(
                f"ALTER TABLE {self.database}.learning_experiences "
                "ADD COLUMN IF NOT EXISTS ingested_at DateTime64(3, 'UTC') DEFAULT timestamp"
            )
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
                    datetime.now(timezone.utc),
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
                    "ingested_at",
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

    def query_closed_experiences(
        self,
        start: datetime,
        end: datetime,
        *,
        sources: Iterable[str] | None = None,
        limit: int = 100_000,
    ) -> list[dict]:
        """Read a bounded, point-in-time window of closed experiences.

        The method is read-only and intentionally separate from the paper/live
        write path. Malformed JSON is an error, not silently discarded evidence.
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("experience query boundaries must be timezone-aware")
        if end <= start:
            raise ValueError("experience query end must be after start")
        if not 1 <= int(limit) <= 1_000_000:
            raise ValueError("experience query limit must be between 1 and 1000000")

        normalized_sources = tuple(sorted({str(item) for item in (sources or ())}))
        allowed_sources = {"backtest", "paper", "live"}
        if any(item not in allowed_sources for item in normalized_sources):
            raise ValueError("experience sources must be backtest, paper or live")

        query = (
            f"SELECT toString(experience_id), timestamp, source, symbol, decision, "
            f"outcome, reward, features_json, market_state_json, risk_state_json, "
            f"strategy_version, model_version, metadata_json "
            f"FROM {self.database}.learning_experiences "  # nosec B608 - trusted database config; values are parameterized
            "WHERE outcome = 'closed' "
            "AND timestamp >= {start:DateTime64(3, 'UTC')} "
            "AND timestamp < {end:DateTime64(3, 'UTC')} "
        )
        parameters = {
            "start": start.astimezone(timezone.utc),
            "end": end.astimezone(timezone.utc),
            "limit": int(limit),
        }
        if normalized_sources:
            query += "AND source IN {sources:Array(String)} "
            parameters["sources"] = list(normalized_sources)
        query += "ORDER BY timestamp, experience_id LIMIT {limit:UInt32}"

        try:
            rows = self.client.query(query, parameters=parameters).result_rows
        except Exception:
            logger.exception(
                "learning experience closed-window query failed",
                extra={
                    "aitos_extra": {
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "sources": normalized_sources,
                        "limit": limit,
                    }
                },
            )
            raise

        import json

        def decode_object(value, column: str) -> dict:
            try:
                decoded = json.loads(value or "{}")
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid {column} JSON in learning_experiences"
                ) from exc
            if not isinstance(decoded, dict):
                raise ValueError(
                    f"{column} must decode to a JSON object in learning_experiences"
                )
            return decoded

        records = []
        for row in rows:
            if len(row) != 13:
                raise ValueError("unexpected learning_experiences query row shape")
            (
                experience_id,
                timestamp,
                source,
                symbol,
                decision,
                outcome,
                reward,
                features_json,
                market_state_json,
                risk_state_json,
                strategy_version,
                model_version,
                metadata_json,
            ) = row
            records.append(
                {
                    "experience_id": str(experience_id),
                    "timestamp": timestamp,
                    "source": str(source),
                    "symbol": str(symbol),
                    "decision": str(decision),
                    "outcome": outcome,
                    "reward": float(reward),
                    "features": decode_object(features_json, "features"),
                    "market_state": decode_object(market_state_json, "market_state"),
                    "risk_state": decode_object(risk_state_json, "risk_state"),
                    "strategy_version": str(strategy_version),
                    "model_version": str(model_version),
                    "metadata": decode_object(metadata_json, "metadata"),
                }
            )
        return records


def _json(value: dict) -> str:
    import json

    return json.dumps(value, sort_keys=True, default=str)
