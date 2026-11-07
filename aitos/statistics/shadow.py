"""Durable shadow-forecast ledger for statistical model validation."""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .contracts import ForecastEnvelope
from .validation import brier_score, log_loss, qlike_loss


@dataclass(frozen=True)
class ShadowMetrics:
    observations: int
    brier: float
    log_loss: float
    qlike: float
    directional_hit_rate: float
    mean_absolute_return_error: float


class StatisticalShadowLedger:
    """Persist forecasts and outcomes without connecting them to execution.

    A forecast identifier is immutable and unique. Repeated identical writes
    are idempotent; conflicting reuse is rejected. Each operation opens its
    own SQLite connection and uses an immediate transaction, making the
    ledger safe for independent worker processes sharing a local database.
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = str(path)
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self.busy_timeout_ms = int(busy_timeout_ms)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS statistical_shadow_forecasts (
                    forecast_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    feature_snapshot_id TEXT NOT NULL,
                    predicted_up REAL NOT NULL,
                    predicted_variance REAL NOT NULL,
                    expected_return REAL NOT NULL,
                    actual_return REAL,
                    realised_variance REAL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_shadow_symbol_horizon_created
                ON statistical_shadow_forecasts(symbol, horizon, created_at)
                """
            )
        finally:
            connection.close()

    @staticmethod
    def _timestamp(value: str | None = None) -> str:
        result = value or datetime.now(timezone.utc).isoformat()
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamps must include a timezone")
        return parsed.astimezone(timezone.utc).isoformat()

    def record_forecast(
        self,
        forecast_id: str,
        envelope: ForecastEnvelope,
        *,
        predicted_up: float,
        predicted_variance: float,
        created_at: str | None = None,
    ) -> bool:
        """Insert a forecast; return False for an identical idempotent replay."""
        if not forecast_id or not forecast_id.strip():
            raise ValueError("forecast_id must not be empty")
        predicted_up = float(predicted_up)
        predicted_variance = float(predicted_variance)
        if not math.isfinite(predicted_up) or not 0.0 <= predicted_up <= 1.0:
            raise ValueError("predicted_up must be finite and in [0, 1]")
        if not math.isfinite(predicted_variance) or predicted_variance <= 0:
            raise ValueError("predicted_variance must be finite and positive")
        created = self._timestamp(created_at)
        values = (
            envelope.symbol, envelope.horizon, envelope.model_id,
            envelope.model_version, envelope.as_of, envelope.feature_snapshot_id,
            predicted_up, predicted_variance, envelope.expected_return, created,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT symbol, horizon, model_id, model_version, as_of, "
                "feature_snapshot_id, predicted_up, predicted_variance, "
                "expected_return FROM statistical_shadow_forecasts "
                "WHERE forecast_id=?",
                (forecast_id,),
            ).fetchone()
            if row is not None:
                if tuple(row) != values[:-1]:
                    raise ValueError("forecast_id already exists with different content")
                connection.execute("COMMIT")
                return False
            connection.execute(
                """
                INSERT INTO statistical_shadow_forecasts (
                    forecast_id, symbol, horizon, model_id, model_version, as_of,
                    feature_snapshot_id, predicted_up, predicted_variance,
                    expected_return, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (forecast_id, *values),
            )
            connection.execute("COMMIT")
            return True
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def record_outcome(
        self,
        forecast_id: str,
        *,
        actual_return: float,
        realised_variance: float,
        resolved_at: str | None = None,
    ) -> bool:
        """Resolve a pending forecast once; identical retries are idempotent."""
        actual_return = float(actual_return)
        realised_variance = float(realised_variance)
        if not math.isfinite(actual_return):
            raise ValueError("actual_return must be finite")
        if not math.isfinite(realised_variance) or realised_variance < 0:
            raise ValueError("realised_variance must be finite and non-negative")
        resolved = self._timestamp(resolved_at)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT actual_return, realised_variance, resolved_at "
                "FROM statistical_shadow_forecasts WHERE forecast_id=?",
                (forecast_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown forecast_id: {forecast_id}")
            if row["actual_return"] is not None:
                if (
                    row["actual_return"] != actual_return
                    or row["realised_variance"] != realised_variance
                ):
                    raise ValueError("forecast outcome is already resolved differently")
                connection.execute("COMMIT")
                return False
            connection.execute(
                "UPDATE statistical_shadow_forecasts "
                "SET actual_return=?, realised_variance=?, resolved_at=? "
                "WHERE forecast_id=? AND actual_return IS NULL",
                (actual_return, realised_variance, resolved, forecast_id),
            )
            connection.execute("COMMIT")
            return True
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def metrics(self, *, symbol: str | None = None,
                horizon: str | None = None) -> ShadowMetrics:
        """Return strictly outcome-resolved shadow metrics for a cohort."""
        clauses = ["actual_return IS NOT NULL", "realised_variance IS NOT NULL"]
        parameters: list[str] = []
        if symbol is not None:
            clauses.append("symbol=?")
            parameters.append(symbol)
        if horizon is not None:
            clauses.append("horizon=?")
            parameters.append(horizon)
        query = (
            "SELECT predicted_up, predicted_variance, expected_return, "
            "actual_return, realised_variance FROM statistical_shadow_forecasts "
            "WHERE " + " AND ".join(clauses)
        )
        connection = self._connect()
        try:
            rows = connection.execute(query, parameters).fetchall()
        finally:
            connection.close()
        if not rows:
            return ShadowMetrics(0, 0.0, 0.0, 0.0, 0.0, 0.0)
        probabilities = [float(row["predicted_up"]) for row in rows]
        outcomes = [int(float(row["actual_return"]) > 0.0) for row in rows]
        actual = [float(row["actual_return"]) for row in rows]
        expected = [float(row["expected_return"]) for row in rows]
        qlike = qlike_loss(
            [float(row["predicted_variance"]) for row in rows],
            [float(row["realised_variance"]) for row in rows],
        )
        return ShadowMetrics(
            observations=len(rows),
            brier=brier_score(probabilities, outcomes),
            log_loss=log_loss(probabilities, outcomes),
            qlike=qlike,
            directional_hit_rate=sum(
                (probability >= 0.5) == bool(outcome)
                for probability, outcome in zip(probabilities, outcomes)
            ) / len(rows),
            mean_absolute_return_error=sum(
                abs(prediction - realised)
                for prediction, realised in zip(expected, actual)
            ) / len(rows),
        )

    def close(self) -> None:
        """Compatibility hook; connections are scoped to individual methods."""
        return None
