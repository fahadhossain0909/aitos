"""Stateful Financial ML lifecycle for scanner shadowing and paper outcomes.

The pipeline owns research labels and model artifacts, never order execution.
It persists candidate identity, event-time features, triple-barrier resolution
and provenance. Executed trades are resolved only from the lifecycle's realised
net outcome; rejected/not-filled candidates are explicitly counterfactual.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from aitos.statistics.financial_ml import (
    MarketEvent,
    MetaLabeler,
    fractional_difference,
    sequential_bootstrap_indices,
)
from aitos.statistics.financial_ml_validation import monte_carlo_stress_test
from aitos.statistics.validation import (
    brier_score,
    expected_calibration_error,
    purged_walk_forward_folds,
)


def _utc(value: Any = None) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        result = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif value is None:
        result = datetime.now(timezone.utc)
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _iso(value: Any = None) -> str:
    return _utc(value).isoformat()


def _finite(value: Any, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


@dataclass(frozen=True)
class FinancialMLPrediction:
    candidate_id: str
    raw_probability: float | None
    calibrated_probability: float | None
    accepted: bool
    mode: str
    reason: str


@dataclass(frozen=True)
class FinancialMLTrainingReport:
    trained: bool
    passed_oos: bool
    samples: int
    folds: int
    oos_samples: int
    brier: float | None
    ece: float | None
    precision_at_threshold: float | None
    base_rate: float | None
    threshold: float
    reason: str
    executed_oos_samples: int = 0
    counterfactual_oos_samples: int = 0
    accepted_executed_mean_return: float | None = None
    accepted_counterfactual_mean_return: float | None = None
    monte_carlo_probability_of_loss: float | None = None
    monte_carlo_terminal_p05: float | None = None
    monte_carlo_max_drawdown_p95: float | None = None
    calibration_samples: int = 0


class FinancialMLPipeline:
    """Durable event sampling, candidate labels, calibration and governed scoring."""

    FEATURE_NAMES = (
        "composite_score",
        "trend_strength",
        "liquidity_quality",
        "order_flow_bias",
        "auction_context",
        "volatility_score",
        "market_regime",
        "lead_lag",
        "funding_rate",
        "open_interest_trend",
        "rl_confidence",
        "footprint_interaction",
        "a_stat",
        "order_book_flow_imbalance",
        "entry_timing",
        "breakout_acceptance",
        "relative_strength",
        "atr_pct",
        "event_volume_imbalance",
        "event_dollar_activity",
        "fractional_return",
        "sv_log_variance",
        "sv_persistence",
        "jump_intensity",
        "jump_variance_fraction",
        "rough_hurst",
    )

    def __init__(
        self,
        database_path: str | Path,
        *,
        mode: str | None = None,
        enforce: bool | None = None,
        volume_bar_threshold: float = 25.0,
        dollar_bar_threshold: float = 250_000.0,
        imbalance_bar_threshold: float = 8.0,
        profit_take_multiple: float = 2.0,
        stop_loss_multiple: float = 1.0,
        max_holding_seconds: float = 900.0,
        round_trip_cost_bps: float = 8.0,
        min_training_samples: int = 50,
        min_oos_samples: int = 20,
        acceptance_threshold: float = 0.55,
    ) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.mode = (mode or os.getenv("AITOS_FINANCIAL_ML_MODE", "shadow")).lower()
        if self.mode not in {"disabled", "shadow", "enforce"}:
            raise ValueError("financial ML mode must be disabled, shadow or enforce")
        self.enforce = (
            self.mode == "enforce"
            if enforce is None
            else bool(enforce) and self.mode == "enforce"
        )
        self.volume_bar_threshold = _finite(volume_bar_threshold, "volume threshold")
        self.dollar_bar_threshold = _finite(dollar_bar_threshold, "dollar threshold")
        self.imbalance_bar_threshold = _finite(
            imbalance_bar_threshold, "imbalance threshold"
        )
        self.profit_take_multiple = _finite(
            profit_take_multiple, "profit take multiple"
        )
        self.stop_loss_multiple = _finite(stop_loss_multiple, "stop loss multiple")
        self.max_holding_seconds = _finite(max_holding_seconds, "max holding seconds")
        self.round_trip_cost_bps = _finite(round_trip_cost_bps, "round trip costs")
        self.min_training_samples = int(min_training_samples)
        self.min_oos_samples = int(min_oos_samples)
        self.acceptance_threshold = _finite(
            acceptance_threshold, "acceptance threshold"
        )
        if (
            min(
                self.volume_bar_threshold,
                self.dollar_bar_threshold,
                self.imbalance_bar_threshold,
                self.profit_take_multiple,
                self.stop_loss_multiple,
                self.max_holding_seconds,
            )
            <= 0
            or self.round_trip_cost_bps < 0
        ):
            raise ValueError("financial ML thresholds and horizons must be positive")
        if self.min_training_samples < 20 or self.min_oos_samples < 10:
            raise ValueError("financial ML sample floors are too small")
        if not 0.0 < self.acceptance_threshold < 1.0:
            raise ValueError("acceptance_threshold must be in (0,1)")
        self._lock = threading.RLock()
        self._models: dict[str, Any] = {}
        self._calibrators: dict[str, Any] = {}
        self._artifact_version: str | None = None
        self._conn = sqlite3.connect(
            str(self.path), timeout=10.0, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS financial_ml_candidates (
                candidate_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                created_at TEXT NOT NULL,
                entry_price REAL NOT NULL,
                volatility REAL NOT NULL,
                take_price REAL NOT NULL,
                stop_price REAL NOT NULL,
                expires_at TEXT NOT NULL,
                features_json TEXT NOT NULL,
                trace_id TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                label INTEGER,
                gross_return REAL,
                net_return REAL,
                label_resolution_time TEXT,
                outcome_provenance TEXT,
                trade_id TEXT,
                outcome_json TEXT,
                ledger_synced INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_financial_ml_pending
            ON financial_ml_candidates(symbol, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_financial_ml_resolved
            ON financial_ml_candidates(status, label_resolution_time);
            CREATE TABLE IF NOT EXISTS financial_ml_event_bars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                bar_type TEXT NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                dollar_volume REAL NOT NULL,
                signed_volume REAL NOT NULL,
                event_count INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_financial_ml_bars
            ON financial_ml_event_bars(symbol, bar_type, end_at);
            CREATE TABLE IF NOT EXISTS financial_ml_event_bar_state (
                symbol TEXT NOT NULL,
                bar_type TEXT NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                dollar_volume REAL NOT NULL,
                signed_volume REAL NOT NULL,
                event_count INTEGER NOT NULL,
                measure_value REAL NOT NULL,
                PRIMARY KEY(symbol, bar_type)
            );
            CREATE TABLE IF NOT EXISTS financial_ml_approvals (
                approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
                artifact_id TEXT NOT NULL,
                approved_by TEXT NOT NULL,
                rationale TEXT NOT NULL,
                approved_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS financial_ml_approvals_no_update
            BEFORE UPDATE ON financial_ml_approvals
            BEGIN SELECT RAISE(ABORT, 'financial ML approvals are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS financial_ml_approvals_no_delete
            BEFORE DELETE ON financial_ml_approvals
            BEGIN SELECT RAISE(ABORT, 'financial ML approvals are append-only'); END;
            CREATE TABLE IF NOT EXISTS financial_ml_artifacts (
                artifact_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                artifact_json TEXT NOT NULL,
                report_json TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        candidate_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(financial_ml_candidates)")
        }
        if "ledger_synced" not in candidate_columns:
            self._conn.execute(
                "ALTER TABLE financial_ml_candidates ADD COLUMN ledger_synced INTEGER NOT NULL DEFAULT 0"
            )
        legacy_expiries = self._conn.execute(
            "SELECT candidate_id,expires_at FROM financial_ml_candidates "
            "WHERE typeof(expires_at) IN ('real','integer')"
        ).fetchall()
        for legacy in legacy_expiries:
            self._conn.execute(
                "UPDATE financial_ml_candidates SET expires_at=? WHERE candidate_id=?",
                (_iso(float(legacy["expires_at"])), legacy["candidate_id"]),
            )
        self._conn.commit()
        self._load_latest_artifact()

    def _load_latest_artifact(self) -> None:
        row = self._conn.execute(
            "SELECT artifact_id,artifact_json FROM financial_ml_artifacts "
            "WHERE active=1 ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            self._models.clear()
            self._calibrators.clear()
            self._artifact_version = None
            return
        artifact = json.loads(row["artifact_json"])
        if artifact.get("feature_names") != list(self.FEATURE_NAMES):
            self._models.clear()
            self._calibrators.clear()
            self._artifact_version = str(row["artifact_id"])
            return
        self._models["meta"] = self._model_from_dict(artifact["model"])
        if artifact.get("calibrator"):
            self._calibrators["platt"] = self._model_from_dict(artifact["calibrator"])
        self._artifact_version = str(row["artifact_id"])

    @staticmethod
    def _model_to_dict(model: MetaLabeler) -> dict[str, Any]:
        return {
            "learning_rate": model.learning_rate,
            "l2": model.l2,
            "epochs": model.epochs,
            "means": list(model.means),
            "scales": list(model.scales),
            "coefficients": list(model.coefficients),
            "intercept": model.intercept,
            "fitted": model.fitted,
        }

    @staticmethod
    def _model_from_dict(data: Mapping[str, Any]) -> MetaLabeler:
        model = MetaLabeler(
            learning_rate=float(data["learning_rate"]),
            l2=float(data["l2"]),
            epochs=int(data["epochs"]),
        )
        model.means = tuple(float(x) for x in data["means"])
        model.scales = tuple(float(x) for x in data["scales"])
        model.coefficients = tuple(float(x) for x in data["coefficients"])
        model.intercept = float(data["intercept"])
        model.fitted = bool(data["fitted"])
        return model

    def ingest_market_event(self, symbol: str, event: MarketEvent) -> dict[str, int]:
        """Incrementally persist event bars and their unfinished tails.

        Each bar type has its own durable accumulator, so the hot path is O(1)
        per trade and restart recovery does not discard an incomplete bar.
        """
        key = symbol.upper()
        dollar = event.price * event.volume
        specs = (
            ("volume", self.volume_bar_threshold, event.volume),
            ("dollar", self.dollar_bar_threshold, dollar),
            ("imbalance", self.imbalance_bar_threshold, event.signed_volume),
        )
        output: dict[str, int] = {}
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for bar_type, threshold, increment in specs:
                    row = self._conn.execute(
                        """SELECT * FROM financial_ml_event_bar_state
                        WHERE symbol=? AND bar_type=?""",
                        (key, bar_type),
                    ).fetchone()
                    if row is None:
                        start_at = end_at = event.timestamp
                        opening = high = low = close = event.price
                        volume = event.volume
                        dollar_volume = dollar
                        signed_volume = event.signed_volume
                        event_count = 1
                        measure_value = increment
                    else:
                        if event.timestamp < row["end_at"]:
                            raise ValueError(
                                "market events must be chronological per symbol"
                            )
                        start_at = row["start_at"]
                        end_at = event.timestamp
                        opening = float(row["open"])
                        high = max(float(row["high"]), event.price)
                        low = min(float(row["low"]), event.price)
                        close = event.price
                        volume = float(row["volume"]) + event.volume
                        dollar_volume = float(row["dollar_volume"]) + dollar
                        signed_volume = (
                            float(row["signed_volume"]) + event.signed_volume
                        )
                        event_count = int(row["event_count"]) + 1
                        measure_value = float(row["measure_value"]) + increment
                    crossed = (
                        abs(measure_value) >= threshold
                        if bar_type == "imbalance"
                        else measure_value >= threshold
                    )
                    if crossed:
                        self._conn.execute(
                            """INSERT INTO financial_ml_event_bars
                            (symbol,bar_type,start_at,end_at,open,high,low,close,
                             volume,dollar_volume,signed_volume,event_count)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                key,
                                bar_type,
                                start_at,
                                end_at,
                                opening,
                                high,
                                low,
                                close,
                                volume,
                                dollar_volume,
                                signed_volume,
                                event_count,
                            ),
                        )
                        self._conn.execute(
                            "DELETE FROM financial_ml_event_bar_state WHERE symbol=? AND bar_type=?",
                            (key, bar_type),
                        )
                        output[bar_type] = 1
                    else:
                        self._conn.execute(
                            """INSERT INTO financial_ml_event_bar_state
                            (symbol,bar_type,start_at,end_at,open,high,low,close,
                             volume,dollar_volume,signed_volume,event_count,measure_value)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(symbol,bar_type) DO UPDATE SET
                            end_at=excluded.end_at,high=excluded.high,low=excluded.low,
                            close=excluded.close,volume=excluded.volume,
                            dollar_volume=excluded.dollar_volume,
                            signed_volume=excluded.signed_volume,
                            event_count=excluded.event_count,
                            measure_value=excluded.measure_value""",
                            (
                                key,
                                bar_type,
                                start_at,
                                end_at,
                                opening,
                                high,
                                low,
                                close,
                                volume,
                                dollar_volume,
                                signed_volume,
                                event_count,
                                measure_value,
                            ),
                        )
                        output[bar_type] = 0
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return output

    def event_features(self, symbol: str) -> dict[str, float]:
        key = symbol.upper()
        with self._lock:
            rows = self._conn.execute(
                """SELECT volume,dollar_volume,signed_volume,close
                FROM financial_ml_event_bars
                WHERE symbol=? AND bar_type='volume'
                ORDER BY end_at DESC LIMIT 20""",
                (key,),
            ).fetchall()
        if not rows:
            return {
                "event_volume_imbalance": 0.0,
                "event_dollar_activity": 0.0,
                "fractional_return": 0.0,
            }
        chronological = list(reversed(rows))
        signed = sum(float(row["signed_volume"]) for row in chronological)
        volume = sum(float(row["volume"]) for row in chronological)
        closes = [float(row["close"]) for row in chronological]
        returns = [
            closes[index] / closes[index - 1] - 1.0
            for index in range(1, len(closes))
            if closes[index - 1] > 0
        ]
        diff = fractional_difference(returns, order=0.4, threshold=1e-3)
        valid = [value for value in diff if value is not None]
        return {
            "event_volume_imbalance": signed / volume if volume > 0 else 0.0,
            "event_dollar_activity": math.log1p(
                sum(float(row["dollar_volume"]) for row in chronological)
            ),
            "fractional_return": float(valid[-1]) if valid else 0.0,
        }

    def register_candidate(
        self,
        *,
        candidate_id: str,
        symbol: str,
        side: str,
        entry_price: float,
        volatility: float,
        features: Mapping[str, float],
        created_at: Any = None,
        trace_id: str | None = None,
    ) -> bool:
        if not candidate_id or not symbol:
            raise ValueError("candidate_id and symbol are required")
        side = str(side).upper()
        if side not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT")
        entry = _finite(entry_price, "entry_price")
        vol = _finite(volatility, "volatility")
        if entry <= 0 or vol <= 0:
            raise ValueError("entry price and volatility must be positive")
        clean = {str(k): _finite(v, str(k)) for k, v in features.items()}
        now = _utc(created_at)
        direction = 1.0 if side == "LONG" else -1.0
        take = entry * (1.0 + direction * self.profit_take_multiple * vol)
        stop = entry * (1.0 - direction * self.stop_loss_multiple * vol)
        if min(take, stop) <= 0:
            raise ValueError("barrier prices must be positive")
        with self._lock:
            cursor = self._conn.execute(
                """INSERT OR IGNORE INTO financial_ml_candidates
                (candidate_id,symbol,side,created_at,entry_price,volatility,
                 take_price,stop_price,expires_at,features_json,trace_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    candidate_id,
                    symbol.upper(),
                    side,
                    now.isoformat(),
                    entry,
                    vol,
                    take,
                    stop,
                    _iso(now.timestamp() + self.max_holding_seconds),
                    json.dumps(clean, sort_keys=True, allow_nan=False),
                    trace_id,
                ),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def reject_candidate(self, candidate_id: str, reason: str) -> bool:
        if not candidate_id or not reason:
            return False
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE financial_ml_candidates SET status='REJECTED', outcome_json=? "
                "WHERE candidate_id=? AND status='PENDING'",
                (
                    json.dumps(
                        {"rejection_reason": str(reason), "counterfactual": True}
                    ),
                    candidate_id,
                ),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def observe_price(self, symbol: str, price: float, timestamp: Any = None) -> int:
        """Resolve unfilled proposals as counterfactual markouts, never realised PnL."""
        value = _finite(price, "price")
        if value <= 0:
            return 0
        now = _utc(timestamp)
        resolved = 0
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM financial_ml_candidates
                WHERE symbol=? AND status='REJECTED' AND created_at<=?
                ORDER BY created_at ASC LIMIT 5000""",
                (symbol.upper(), now.isoformat()),
            ).fetchall()
            for row in rows:
                side = 1.0 if row["side"] == "LONG" else -1.0
                take_hit = (
                    value >= row["take_price"]
                    if side > 0
                    else value <= row["take_price"]
                )
                stop_hit = (
                    value <= row["stop_price"]
                    if side > 0
                    else value >= row["stop_price"]
                )
                expired = now >= _utc(row["expires_at"])
                if not (take_hit or stop_hit or expired):
                    continue
                gross = side * (value / float(row["entry_price"]) - 1.0)
                net = gross - self.round_trip_cost_bps / 10_000.0
                label = 1 if net > 0 else 0
                barrier = "take" if take_hit else "stop" if stop_hit else "vertical"
                outcome = {
                    "barrier": barrier,
                    "counterfactual": True,
                    "replay_type": "live_market_markout",
                    "gross_return": gross,
                    "net_return": net,
                    "cost_bps": self.round_trip_cost_bps,
                    "entry_price": float(row["entry_price"]),
                    "observed_price": value,
                }
                cursor = self._conn.execute(
                    """UPDATE financial_ml_candidates SET status='RESOLVED',
                    label=?,gross_return=?,net_return=?,label_resolution_time=?,
                    outcome_provenance='COUNTERFACTUAL_LIVE_MARKOUT',outcome_json=?
                    WHERE candidate_id=? AND status='REJECTED'""",
                    (
                        label,
                        gross,
                        net,
                        now.isoformat(),
                        json.dumps(outcome, sort_keys=True),
                        row["candidate_id"],
                    ),
                )
                resolved += max(0, cursor.rowcount)
            self._conn.commit()
        return resolved

    def pending_counterfactual_labels(
        self, limit: int = 500
    ) -> tuple[dict[str, Any], ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._lock:
            rows = self._conn.execute(
                """SELECT candidate_id,symbol,side,label,net_return,
                label_resolution_time,outcome_provenance,outcome_json
                FROM financial_ml_candidates
                WHERE status='RESOLVED' AND outcome_provenance LIKE 'COUNTERFACTUAL%'
                  AND ledger_synced=0
                ORDER BY label_resolution_time ASC LIMIT ?""",
                (limit,),
            ).fetchall()
        return tuple(
            {
                **dict(row),
                "outcome": json.loads(row["outcome_json"] or "{}"),
            }
            for row in rows
        )

    def mark_ledger_synced(self, candidate_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                """UPDATE financial_ml_candidates SET ledger_synced=1
                WHERE candidate_id=? AND status='RESOLVED'
                  AND outcome_provenance LIKE 'COUNTERFACTUAL%'""",
                (candidate_id,),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def mark_executed(self, candidate_id: str, trade_id: str) -> bool:
        if not candidate_id or not trade_id:
            return False
        with self._lock:
            cursor = self._conn.execute(
                """UPDATE financial_ml_candidates SET status='EXECUTED',trade_id=?,
                outcome_json=NULL
                WHERE candidate_id=? AND status IN ('PENDING','REJECTED')
                  AND label IS NULL""",
                (trade_id, candidate_id),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def resolve_executed(
        self,
        candidate_id: str,
        *,
        net_return: float,
        resolved_at: Any,
        trade_id: str,
        outcome: Mapping[str, Any] | None = None,
    ) -> bool:
        value = _finite(net_return, "net_return")
        if not candidate_id or not trade_id:
            raise ValueError("candidate and trade identities are required")
        now = _utc(resolved_at)
        label = 1 if value > 0 else 0
        body = {
            **dict(outcome or {}),
            "counterfactual": False,
            "provenance": "EXECUTED_TRADE_NET_OUTCOME",
            "trade_id": trade_id,
            "net_return": value,
        }
        with self._lock:
            row = self._conn.execute(
                "SELECT status,trade_id FROM financial_ml_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                return False
            if row["status"] == "RESOLVED":
                existing = self._conn.execute(
                    "SELECT label,net_return,trade_id FROM financial_ml_candidates WHERE candidate_id=?",
                    (candidate_id,),
                ).fetchone()
                if (
                    existing["trade_id"] == trade_id
                    and existing["net_return"] is not None
                    and abs(float(existing["net_return"]) - value) < 1e-12
                ):
                    return False
                raise ValueError("candidate already resolved with different outcome")
            if row["status"] != "EXECUTED" or row["trade_id"] != trade_id:
                raise ValueError("candidate is not bound to this executed trade")
            self._conn.execute(
                """UPDATE financial_ml_candidates SET status='RESOLVED',label=?,
                net_return=?,gross_return=?,label_resolution_time=?,
                outcome_provenance='EXECUTED_TRADE_NET_OUTCOME',outcome_json=?
                WHERE candidate_id=?""",
                (
                    label,
                    value,
                    (
                        _finite(body["gross_return"], "gross_return")
                        if body.get("gross_return") is not None
                        else None
                    ),
                    now.isoformat(),
                    json.dumps(body, sort_keys=True, allow_nan=False),
                    candidate_id,
                ),
            )
            self._conn.commit()
            return True

    @staticmethod
    def _logit(probability: float) -> float:
        p = min(1.0 - 1e-8, max(1e-8, probability))
        return math.log(p / (1.0 - p))

    def train_walk_forward(self) -> FinancialMLTrainingReport:
        """Train chronologically, purge unresolved overlapping labels and calibrate OOF."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT candidate_id,created_at,expires_at,features_json,label,
                net_return,label_resolution_time,outcome_provenance FROM financial_ml_candidates
                WHERE status='RESOLVED' AND label IS NOT NULL
                ORDER BY created_at ASC"""
            ).fetchall()
        samples = len(rows)
        if samples < self.min_training_samples:
            return FinancialMLTrainingReport(
                False,
                False,
                samples,
                0,
                0,
                None,
                None,
                None,
                None,
                self.acceptance_threshold,
                "insufficient_resolved_samples",
            )
        features = [
            [
                float(json.loads(row["features_json"]).get(name, 0.0))
                for name in self.FEATURE_NAMES
            ]
            for row in rows
        ]
        labels = [int(row["label"]) for row in rows]
        folds = purged_walk_forward_folds(
            samples,
            n_splits=min(5, max(2, samples // 20)),
            label_horizon=1,
            embargo=1,
        )
        oos_probabilities: list[float] = []
        oos_labels: list[int] = []
        oos_returns: list[float] = []
        oos_provenance: list[str] = []
        oos_indices: list[int] = []
        used_folds = 0
        starts = list(range(samples))
        ends = [
            max(
                index,
                next(
                    (
                        j
                        for j in range(index, samples)
                        if rows[j]["created_at"] >= rows[index]["expires_at"]
                    ),
                    index,
                ),
            )
            for index in range(samples)
        ]
        for fold in folds:
            test_start = fold.test_indices[0]
            test_time = rows[test_start]["created_at"]
            train_indices = [
                index
                for index in fold.train_indices
                if rows[index]["label_resolution_time"] is not None
                and rows[index]["label_resolution_time"] < test_time
            ]
            if (
                len(train_indices) < 10
                or len({labels[index] for index in train_indices}) < 2
            ):
                continue
            # Sequential bootstrap reduces overlap concentration within each
            # chronological training fold; purge remains the primary leakage guard.
            selected_local = sequential_bootstrap_indices(
                [starts[index] for index in train_indices],
                [ends[index] for index in train_indices],
                sample_count=len(train_indices),
                seed=fold.test_indices[0],
            )
            sampled = [train_indices[index] for index in selected_local]
            model = MetaLabeler(learning_rate=0.03, l2=0.01, epochs=600)
            model.fit(
                [features[index] for index in sampled],
                [labels[index] for index in sampled],
            )
            probabilities = model.predict_probability(
                [features[index] for index in fold.test_indices]
            )
            oos_probabilities.extend(probabilities)
            oos_labels.extend(labels[index] for index in fold.test_indices)
            oos_returns.extend(
                float(rows[index]["net_return"] or 0.0) for index in fold.test_indices
            )
            oos_provenance.extend(
                str(rows[index]["outcome_provenance"] or "UNKNOWN")
                for index in fold.test_indices
            )
            oos_indices.extend(fold.test_indices)
            used_folds += 1
        calibration_size = max(10, len(oos_labels) // 3)
        evaluation_labels = oos_labels[calibration_size:]
        evaluation_indices = oos_indices[calibration_size:]
        if (
            len(oos_labels) < calibration_size + self.min_oos_samples
            or len(set(oos_labels[:calibration_size])) < 2
            or len(set(evaluation_labels)) < 2
        ):
            return FinancialMLTrainingReport(
                False,
                False,
                samples,
                used_folds,
                len(evaluation_labels),
                None,
                None,
                None,
                sum(labels) / samples,
                self.acceptance_threshold,
                "insufficient_disjoint_calibration_and_oos_samples",
                calibration_samples=calibration_size,
            )
        # Fit Platt scaling on the early walk-forward OOF segment, then
        # evaluate calibration/acceptance only on a later untouched OOF segment.
        calibrator = MetaLabeler(learning_rate=0.02, l2=0.01, epochs=800)
        calibrator.fit(
            [[self._logit(value)] for value in oos_probabilities[:calibration_size]],
            oos_labels[:calibration_size],
        )
        evaluation_probabilities = oos_probabilities[calibration_size:]
        calibrated_oos = calibrator.predict_probability(
            [[self._logit(value)] for value in evaluation_probabilities]
        )
        oos_labels = evaluation_labels
        oos_returns = oos_returns[calibration_size:]
        oos_provenance = oos_provenance[calibration_size:]
        brier = brier_score(calibrated_oos, oos_labels)
        ece = expected_calibration_error(calibrated_oos, oos_labels, n_bins=10)
        selected = [
            index
            for index, value in enumerate(calibrated_oos)
            if value >= self.acceptance_threshold
        ]
        precision = (
            sum(oos_labels[index] for index in selected) / len(selected)
            if selected
            else 0.0
        )
        base_rate = sum(oos_labels) / len(oos_labels)
        selected_returns = [oos_returns[index] for index in selected]
        mean_selected_return = (
            sum(selected_returns) / len(selected_returns) if selected_returns else -1.0
        )
        monte_carlo = None
        if len(selected_returns) >= 20:
            try:
                monte_carlo = monte_carlo_stress_test(
                    selected_returns,
                    paths=250,
                    horizon=max(20, len(selected_returns)),
                    block_length=min(5, len(selected_returns)),
                    seed=17,
                    additional_cost_bps=5.0,
                )
            except ValueError:
                # A <= -100% observation is a ruin outcome; it must fail
                # acceptance, not crash the research/reporting pipeline.
                monte_carlo = None
        executed_oos_samples = sum(
            provenance == "EXECUTED_TRADE_NET_OUTCOME" for provenance in oos_provenance
        )
        counterfactual_oos_samples = sum(
            provenance.startswith("COUNTERFACTUAL") for provenance in oos_provenance
        )
        accepted_executed = [
            oos_returns[index]
            for index in selected
            if oos_provenance[index] == "EXECUTED_TRADE_NET_OUTCOME"
        ]
        accepted_counterfactual = [
            oos_returns[index]
            for index in selected
            if oos_provenance[index].startswith("COUNTERFACTUAL")
        ]
        accepted_executed_mean = (
            sum(accepted_executed) / len(accepted_executed)
            if accepted_executed
            else None
        )
        accepted_counterfactual_mean = (
            sum(accepted_counterfactual) / len(accepted_counterfactual)
            if accepted_counterfactual
            else None
        )
        provenance_floor = max(5, self.min_oos_samples // 4)
        evaluation_cutoff = rows[evaluation_indices[0]]["created_at"]
        final_train_indices = [
            index
            for index, row in enumerate(rows)
            if row["created_at"] < evaluation_cutoff
            and row["label_resolution_time"] is not None
            and row["label_resolution_time"] < evaluation_cutoff
        ]
        final_training_ready = (
            len(final_train_indices) >= self.min_training_samples
            and len({labels[index] for index in final_train_indices}) == 2
        )
        passed = (
            final_training_ready
            and len(oos_labels) >= self.min_oos_samples
            and ece <= 0.15
            and precision > base_rate
            and mean_selected_return > 0.0
            and executed_oos_samples >= provenance_floor
            and counterfactual_oos_samples >= provenance_floor
            and accepted_executed_mean is not None
            and accepted_executed_mean > 0.0
            and accepted_counterfactual_mean is not None
            and accepted_counterfactual_mean > 0.0
            and monte_carlo is not None
            and monte_carlo.probability_of_loss <= 0.50
            and monte_carlo.max_drawdown_p95 <= 0.35
            and bool(selected)
        )
        reason = (
            "oos_calibration_and_acceptance_passed"
            if passed
            else (
                "insufficient_final_training_data_before_holdout"
                if not final_training_ready
                else "oos_acceptance_not_proven"
            )
        )
        if not final_training_ready:
            return FinancialMLTrainingReport(
                False,
                False,
                samples,
                used_folds,
                len(oos_labels),
                brier,
                ece,
                precision,
                base_rate,
                self.acceptance_threshold,
                reason,
                executed_oos_samples,
                counterfactual_oos_samples,
                accepted_executed_mean,
                accepted_counterfactual_mean,
                monte_carlo.probability_of_loss if monte_carlo else None,
                monte_carlo.terminal_return_p05 if monte_carlo else None,
                monte_carlo.max_drawdown_p95 if monte_carlo else None,
                calibration_size,
            )
        final_model = MetaLabeler(learning_rate=0.03, l2=0.01, epochs=800)
        final_model.fit(
            [features[index] for index in final_train_indices],
            [labels[index] for index in final_train_indices],
        )
        artifact_id = (
            f"financial-ml-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
        )
        artifact = {
            "model": self._model_to_dict(final_model),
            "calibrator": self._model_to_dict(calibrator),
            "feature_names": list(self.FEATURE_NAMES),
            "final_training_samples": len(final_train_indices),
            "holdout_start": evaluation_cutoff,
            "threshold": self.acceptance_threshold,
            "oos_samples": len(oos_labels),
            "calibration_samples": calibration_size,
            "executed_oos_samples": executed_oos_samples,
            "counterfactual_oos_samples": counterfactual_oos_samples,
            "accepted_executed_mean_return": accepted_executed_mean,
            "accepted_counterfactual_mean_return": accepted_counterfactual_mean,
            "monte_carlo_probability_of_loss": (
                monte_carlo.probability_of_loss if monte_carlo else None
            ),
            "monte_carlo_terminal_p05": (
                monte_carlo.terminal_return_p05 if monte_carlo else None
            ),
            "monte_carlo_max_drawdown_p95": (
                monte_carlo.max_drawdown_p95 if monte_carlo else None
            ),
            "brier": brier,
            "ece": ece,
        }
        report = FinancialMLTrainingReport(
            True,
            passed,
            samples,
            used_folds,
            len(oos_labels),
            brier,
            ece,
            precision,
            base_rate,
            self.acceptance_threshold,
            reason,
            executed_oos_samples,
            counterfactual_oos_samples,
            accepted_executed_mean,
            accepted_counterfactual_mean,
            monte_carlo.probability_of_loss if monte_carlo else None,
            monte_carlo.terminal_return_p05 if monte_carlo else None,
            monte_carlo.max_drawdown_p95 if monte_carlo else None,
            calibration_size,
        )
        with self._lock:
            self._conn.execute("UPDATE financial_ml_artifacts SET active=0")
            self._conn.execute(
                """INSERT INTO financial_ml_artifacts
                (artifact_id,created_at,artifact_json,report_json,active)
                VALUES (?,?,?,?,?)""",
                (
                    artifact_id,
                    _iso(),
                    json.dumps(artifact, sort_keys=True),
                    json.dumps(asdict(report), sort_keys=True),
                    0,
                ),
            )
            self._conn.commit()
        # Training never promotes itself. An operator must explicitly approve
        # the OOS-passing artifact through approve_artifact().
        self._pending_artifact_id = artifact_id
        return report

    def approve_artifact(
        self,
        artifact_id: str,
        *,
        approved_by: str,
        rationale: str,
        approval_verifier: Callable[[str, str, str], bool] | None = None,
    ) -> bool:
        """Activate an OOS-passing artifact after trusted middleware verification.

        The verifier must validate the operator against the deployment's
        authenticated identity/RBAC boundary. A caller-supplied display name
        alone is never sufficient to approve an artifact.
        """
        if (
            not artifact_id
            or not approved_by.strip()
            or not 8 <= len(rationale.strip()) <= 500
        ):
            raise ValueError(
                "artifact, operator and 8-500 character rationale are required"
            )
        if not callable(approval_verifier) or not approval_verifier(
            artifact_id, approved_by.strip(), rationale.strip()
        ):
            raise PermissionError("trusted operator approval verification is required")
        with self._lock:
            row = self._conn.execute(
                "SELECT artifact_json,report_json FROM financial_ml_artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown financial ML artifact")
            report = json.loads(row["report_json"])
            artifact = json.loads(row["artifact_json"])
            if not report.get("passed_oos"):
                raise ValueError("artifact did not pass out-of-sample acceptance")
            if artifact.get("feature_names") != list(self.FEATURE_NAMES):
                raise ValueError(
                    "artifact feature schema is incompatible with this runtime"
                )
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute("UPDATE financial_ml_artifacts SET active=0")
            self._conn.execute(
                "UPDATE financial_ml_artifacts SET active=1 WHERE artifact_id=?",
                (artifact_id,),
            )
            self._conn.execute(
                "INSERT INTO financial_ml_approvals(artifact_id,approved_by,rationale,approved_at) VALUES(?,?,?,?)",
                (artifact_id, approved_by.strip(), rationale.strip(), _iso()),
            )
            self._conn.commit()
            self._models["meta"] = self._model_from_dict(artifact["model"])
            self._calibrators["platt"] = self._model_from_dict(artifact["calibrator"])
            self._artifact_version = artifact_id
            return True

    def predict(
        self, candidate_id: str, features: Mapping[str, float]
    ) -> FinancialMLPrediction:
        if self.mode == "disabled":
            return FinancialMLPrediction(
                candidate_id, None, None, True, self.mode, "disabled"
            )
        with self._lock:
            active = self._conn.execute(
                "SELECT artifact_id FROM financial_ml_artifacts WHERE active=1 "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            active_id = str(active["artifact_id"]) if active is not None else None
            if active_id != self._artifact_version:
                self._load_latest_artifact()
            model = self._models.get("meta")
            calibrator = self._calibrators.get("platt")
            if model is None or calibrator is None:
                return FinancialMLPrediction(
                    candidate_id, None, None, False, self.mode, "no_oos_approved_model"
                )
            vector = [
                [_finite(features.get(name, 0.0), name) for name in self.FEATURE_NAMES]
            ]
            raw = model.predict_probability(vector)[0]
            calibrated = calibrator.predict_probability([[self._logit(raw)]])[0]
            accepted = calibrated >= self.acceptance_threshold
            reason = (
                "meta_acceptance_threshold_met"
                if accepted
                else "meta_probability_below_threshold"
            )
            return FinancialMLPrediction(
                candidate_id, raw, calibrated, accepted, self.mode, reason
            )

    def should_block(self, prediction: FinancialMLPrediction) -> bool:
        # Enforce is opt-in and requires an active artifact that passed OOS
        # acceptance. Even then it is only an additional veto, never a bypass.
        return self.enforce and (
            prediction.calibrated_probability is None or not prediction.accepted
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counts = {
                str(row["status"]): int(row["count"])
                for row in self._conn.execute(
                    "SELECT status,COUNT(*) AS count FROM financial_ml_candidates GROUP BY status"
                ).fetchall()
            }
            bars = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM financial_ml_event_bars"
                ).fetchone()[0]
            )
            active = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM financial_ml_artifacts WHERE active=1"
                ).fetchone()[0]
            )
        return {
            "mode": self.mode,
            "enforce": self.enforce,
            "candidate_states": counts,
            "event_bars": bars,
            "active_oos_approved_artifacts": active,
            "artifact_version": self._artifact_version,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
