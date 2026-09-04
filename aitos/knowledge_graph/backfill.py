"""Controlled ClickHouse -> Neo4j semantic reconstruction.

This job is deliberately separate from the live runtime. It replays only
semantic analytics events, in bounded batches, and uses MERGE-backed graph
projection so rerunning a window is safe.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .writer import PROJECT_SEMANTIC_EVENT_QUERY, _evidence, _first, _number

SEMANTIC_CATEGORIES = (
    "decision.%",
    "risk.%",
    "scanner.%",
    "statistics.%",
    "intelligence.%",
    "journey.%",
    "execution.%",
)

SELECT_SQL = """
SELECT event_time, ingest_time, event_id, category, symbol, source_module,
       schema_version, payload_json
FROM {database}.live_analytics_events
WHERE event_time >= {start:DateTime64(3)}
  AND event_time < {end:DateTime64(3)}
  AND category LIKE {c0:String}
ORDER BY event_time ASC, event_id ASC
LIMIT {batch_size:UInt32}
"""


class ClickHouseNeo4jBackfill:
    """Bounded, resumable-by-window semantic reconstruction job."""

    def __init__(self, clickhouse_client: Any, neo4j_driver: Any, database: str = "aitos") -> None:
        self._ch = clickhouse_client
        self._neo4j = neo4j_driver
        self._database = database

    async def run(
        self,
        *,
        start: datetime,
        end: datetime,
        batch_size: int = 500,
        max_batches: int | None = None,
    ) -> int:
        batch_size = max(1, min(int(batch_size), 5000))
        cursor_time = start
        cursor_id = ""
        total = 0
        batches = 0

        while cursor_time < end:
            if max_batches is not None and batches >= max_batches:
                break
            result = await self._ch.query(
                SELECT_SQL.format(database=self._database),
                parameters={
                    "start": cursor_time,
                    "end": end,
                    "c0": SEMANTIC_CATEGORIES[0],
                    "batch_size": batch_size,
                },
            )
            rows = result.result_rows
            if not rows:
                break

            # The query uses one LIKE predicate for portability. Filter the
            # remaining semantic namespaces in Python before graph writes.
            rows = [row for row in rows if self._is_semantic_category(str(row[3]))]
            if not rows:
                # Advance from the raw page to avoid getting stuck on a batch
                # containing only non-semantic categories.
                raw = result.result_rows
                cursor_time = raw[-1][0]
                cursor_id = str(raw[-1][2])
                continue

            async with self._neo4j.session() as session:
                for row in rows:
                    params = self._params(row)
                    await session.run(PROJECT_SEMANTIC_EVENT_QUERY, **params)
                    total += 1

            last = result.result_rows[-1]
            next_time, next_id = last[0], str(last[2])
            if next_time == cursor_time and next_id == cursor_id:
                break
            cursor_time, cursor_id = next_time, next_id
            batches += 1
            if len(result.result_rows) < batch_size:
                break

        return total

    @staticmethod
    def _is_semantic_category(category: str) -> bool:
        return any(category.startswith(prefix[:-1]) for prefix in SEMANTIC_CATEGORIES)

    @staticmethod
    def _params(row: tuple[Any, ...]) -> dict[str, Any]:
        event_time, _ingest_time, event_id, category, symbol, source_module, schema_version, payload_json = row
        try:
            payload = json.loads(payload_json) if payload_json else {}
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        event_id = str(event_id)
        return {
            "event_id": event_id,
            "topic": str(category),
            "event_time": event_time.isoformat() if hasattr(event_time, "isoformat") else str(event_time),
            "source_module": str(source_module or "unknown"),
            "schema_version": str(schema_version or payload.get("schema_version", "1")),
            "payload_json": json.dumps(payload, default=str, separators=(",", ":")),
            "symbol": _first(payload, "symbol", "instrument", "market_symbol") or str(symbol or ""),
            "strategy_id": _first(payload, "strategy_id", "strategy", "strategy_version"),
            "model_id": _first(payload, "model_id", "model", "model_version"),
            "policy_id": _first(payload, "policy_id", "policy", "policy_version"),
            "trade_id": _first(payload, "trade_id", "position_id"),
            "decision_id": _first(payload, "decision_id", "decision_idempotency_key"),
            "regime": _first(payload, "regime", "market_regime", "regime_name"),
            "risk_id": _first(payload, "risk_decision_id", "risk_id"),
            "risk_state": _first(payload, "risk_state", "state"),
            "risk_action": _first(payload, "risk_action", "action"),
            "risk_score": _number(payload, "risk_score", "risk_confidence"),
            "execution_id": _first(payload, "execution_id", "fill_id", "order_id"),
            "execution_side": _first(payload, "side", "execution_side"),
            "execution_status": _first(payload, "execution_status", "status"),
            "execution_price": _number(payload, "execution_price", "fill_price", "price"),
            "execution_quantity": _number(payload, "execution_quantity", "fill_quantity", "quantity"),
            "journey_id": _first(payload, "journey_id", "trade_journey_id"),
            "journey_state": _first(payload, "journey_state", "journey_status"),
            "forecast_id": _first(payload, "forecast_id", "prediction_id"),
            "forecast_probability": _number(payload, "probability", "forecast_probability", "confidence"),
            "forecast_target": _first(payload, "target", "forecast_target"),
            "forecast_horizon": _first(payload, "horizon", "forecast_horizon"),
            "outcome_id": _first(payload, "outcome_id", "result_id"),
            "outcome_label": _first(payload, "outcome", "outcome_label", "result"),
            "outcome_pnl": _number(payload, "pnl", "outcome_pnl", "reward"),
            "run_id": _first(payload, "model_run_id", "run_id", "training_run_id"),
            "dataset_id": _first(payload, "dataset_id", "training_dataset_id"),
            "feature_set_version": _first(payload, "feature_set_version", "features_version"),
            "artifact_id": _first(payload, "artifact_id", "model_artifact_id"),
            "run_status": _first(payload, "run_status", "training_status"),
            "calibration_id": _first(payload, "calibration_id", "calibration_run_id"),
            "calibration_method": _first(payload, "calibration_method", "method"),
            "sample_count": _number(payload, "sample_count", "n_samples"),
            "brier": _number(payload, "brier", "brier_score"),
            "log_loss": _number(payload, "log_loss"),
            "ece": _number(payload, "ece", "expected_calibration_error"),
            "evidence": _evidence(payload, event_id),
        }
