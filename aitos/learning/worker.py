"""Durable, single-owner continual-learning worker with keyset pagination."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Any

import clickhouse_connect

from aitos.config.paths import get_model_data_dir, resolve_project_path
from aitos.intelligence.deep_rl_policy import DeepValueRLScorer
from aitos.learning.regime_feedback import RegimeRewardCalibrator, normalize_regime

logger = logging.getLogger(__name__)


class ContinualLearningWorker:
    """Poll all eligible historical experiences without a rolling lookback cutoff.

    The durable cursor is advanced only after an experience has been applied (or
    explicitly rejected as unusable). The model checkpoint records experience IDs
    in the same atomic pickle replacement as the model update, closing the crash
    window between model persistence and cursor persistence.
    """

    def __init__(
        self,
        *,
        host: str = "localhost",
        port: int = 8123,
        user: str = "default",
        password: str = "",
        database: str = "aitos",
        state_path: str | None = None,
        model_path: str | None = None,
        lookback_hours: int | None = None,
        batch_limit: int = 5000,
        poll_seconds: int = 60,
    ) -> None:
        if batch_limit < 1:
            raise ValueError("batch_limit must be >= 1")
        if poll_seconds < 1:
            raise ValueError("poll_seconds must be >= 1")
        self._stop = threading.Event()
        self._closed = False
        self._lock_handle = None
        model_root = get_model_data_dir() / "online_rl"
        state_file = (
            resolve_project_path(state_path)
            if state_path
            else model_root / "worker_state.json"
        )
        model_file = (
            resolve_project_path(model_path)
            if model_path
            else model_root / "deep_value.pkl"
        )
        for path in (state_file, model_file):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.state_path = Path(state_file)
        self.batch_limit = int(batch_limit)
        self.poll_seconds = int(poll_seconds)
        # Kept as a compatibility attribute; historical training is intentionally
        # not truncated by a wall-clock lookback window.
        self.lookback = None
        self.database = database
        self.client = clickhouse_connect.get_client(
            host=host, port=port, username=user, password=password, database=database
        )
        try:
            self.scorer = DeepValueRLScorer(state_path=str(model_file))
            self.scorer.load_state(str(model_file))
            self._cursor_timestamp = "1970-01-01 00:00:00.000"
            self._cursor_experience_id = "00000000-0000-0000-0000-000000000000"
            self._processed = 0
            self._legacy_processed: set[str] = set()
            self._regime_scorers: dict[str, DeepValueRLScorer] = {}
            self._reward_calibrator = RegimeRewardCalibrator()
            self._acquire_singleton_lock()
            self._load_state()
        except BaseException:
            # All setup after client acquisition is owned here, including model
            # loading and singleton acquisition, not only cursor restoration.
            try:
                self.close()
            except Exception as cleanup_error:
                logger.error(
                    "Learning startup cleanup failed (%s)",
                    type(cleanup_error).__name__,
                )
            raise

    def _acquire_singleton_lock(self) -> None:
        lock_path = self.state_path.with_suffix(self.state_path.suffix + ".owner.lock")
        handle = lock_path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise RuntimeError(
                f"another continual-learning worker owns state file {self.state_path}"
            ) from exc
        self._lock_handle = handle

    def _release_singleton_lock(self) -> None:
        handle, self._lock_handle = self._lock_handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._cursor_timestamp = str(
                state.get("cursor_timestamp", self._cursor_timestamp)
            )
            self._cursor_experience_id = str(
                state.get("cursor_experience_id", self._cursor_experience_id)
            )
            self._processed = int(state.get("processed_count", 0))
            if not hasattr(self, "_reward_calibrator"):
                self._reward_calibrator = RegimeRewardCalibrator()
            self._reward_calibrator.restore(state.get("regime_reward_history", {}))
            if "cursor_timestamp" not in state:
                self._legacy_processed = set(
                    str(value) for value in state.get("processed_experiences", [])
                )
        except (OSError, ValueError, TypeError):
            logger.exception("learning cursor state is invalid; refusing unsafe reset")
            raise RuntimeError(f"cannot safely load learning state: {self.state_path}")

    def _save_state(self) -> None:
        payload = {
            "version": 2,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "cursor_timestamp": self._cursor_timestamp,
            "cursor_experience_id": self._cursor_experience_id,
            "processed_count": self._processed,
            "processed_experiences": sorted(self._legacy_processed),
            "n_samples_seen": self.scorer.n_samples_seen
            + sum(
                scorer.n_samples_seen
                for scorer in getattr(self, "_regime_scorers", {}).values()
            ),
            "regime_reward_history": getattr(
                self, "_reward_calibrator", RegimeRewardCalibrator()
            ).snapshot(),
        }
        tmp = self.state_path.with_suffix(
            self.state_path.suffix + f".{os.getpid()}.tmp"
        )
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def _rows(self) -> list[dict[str, Any]]:
        sql = """
        SELECT toString(experience_id) AS experience_id,
               ingested_at AS timestamp, source, symbol, decision, outcome, reward,
               features_json, risk_state_json, strategy_version, metadata_json
        FROM learning_experiences
        WHERE source IN ('backtest', 'paper', 'live')
          AND (ingested_at > {cursor_timestamp:DateTime64(3)}
               OR (ingested_at = {cursor_timestamp:DateTime64(3)}
                   AND toString(experience_id) > {cursor_experience_id:String}))
        ORDER BY ingested_at ASC, toString(experience_id) ASC
        LIMIT {limit:UInt32}
        """
        result = self.client.query(
            sql,
            parameters={
                "cursor_timestamp": datetime.fromisoformat(
                    self._cursor_timestamp.replace(" ", "T")
                ).replace(tzinfo=timezone.utc),
                "cursor_experience_id": self._cursor_experience_id,
                "limit": self.batch_limit,
            },
        )
        return [dict(zip(result.column_names, row)) for row in result.result_rows]

    @staticmethod
    def _numeric_features(features: Any) -> dict[str, float]:
        try:
            value = (
                json.loads(features or "{}")
                if not isinstance(features, dict)
                else features
            )
        except (TypeError, ValueError):
            return {}
        if not isinstance(value, dict):
            return {}
        result: dict[str, float] = {}
        excluded = {
            "risk_amount_usd",
            "initial_risk_usd",
            "position_size_usd",
            "pnl",
            "realized_pnl",
            "reward",
            "exit_price",
            "r_multiple",
        }
        for key, raw in value.items():
            if str(key) not in excluded and isinstance(raw, (int, float, bool)):
                number = float(raw)
                if number == number and abs(number) != float("inf"):
                    result[str(key)] = number
        # rl_confidence is deliberately a constant neutral input for both
        # training and inference; never train on the model's own prior output.
        if not result:
            return {}
        result["rl_confidence"] = 5.0
        return result

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @classmethod
    def _reward_r_multiple(cls, row: dict[str, Any]) -> float | None:
        """Validate trade-level reward units and return a finite R multiple."""
        source = str(row.get("source", "")).strip().lower()
        decision = str(row.get("decision", "")).strip().lower()
        if row.get("outcome") != "closed":
            return None
        if decision != "backtest_trade" and source not in {"paper", "live"}:
            return None
        try:
            reward = float(row.get("reward"))
        except (TypeError, ValueError):
            return None
        if not isfinite(reward):
            return None

        metadata = cls._json_object(row.get("metadata_json"))
        risk_state = cls._json_object(row.get("risk_state_json"))
        features = cls._json_object(row.get("features_json"))
        unit = str(metadata.get("reward_unit", "")).strip().lower()
        if unit in {"r", "r_multiple", "risk_multiple"}:
            return reward
        if unit not in {"", "quote_pnl", "quote_currency", "usd"}:
            return None
        if source in {"paper", "live"} and not unit:
            # Legacy paper/live rows may contain quote PnL; quarantine unless
            # the reward unit is explicit.
            return None

        raw_risk = (
            risk_state.get("risk_amount_usd")
            or risk_state.get("initial_risk_usd")
            or features.get("risk_amount_usd")
            or features.get("initial_risk_usd")
        )
        try:
            risk_amount = float(raw_risk)
        except (TypeError, ValueError):
            return None
        if not isfinite(risk_amount) or risk_amount <= 0:
            return None
        normalized = reward / risk_amount
        return normalized if isfinite(normalized) else None

    def _scorer_for_regime(self, regime: str) -> DeepValueRLScorer:
        """Use an independent durable checkpoint for each market regime."""
        key = normalize_regime(regime)
        base_path = getattr(self.scorer, "_state_path", None)
        if key == "unknown" or base_path is None:
            return self.scorer
        if not hasattr(self, "_regime_scorers"):
            self._regime_scorers = {}
        cached = self._regime_scorers.get(key)
        if cached is not None:
            return cached
        model_path = Path(base_path).parent / "regimes" / f"{key}.pkl"
        model_path.parent.mkdir(parents=True, exist_ok=True)
        scorer = DeepValueRLScorer(state_path=str(model_path))
        scorer.load_state(str(model_path))
        self._regime_scorers[key] = scorer
        return scorer

    @staticmethod
    def _row_regime(row: dict[str, Any]) -> str:
        metadata = ContinualLearningWorker._json_object(row.get("metadata_json"))
        risk_state = ContinualLearningWorker._json_object(row.get("risk_state_json"))
        features = ContinualLearningWorker._json_object(row.get("features_json"))
        return normalize_regime(
            metadata.get("market_regime")
            or metadata.get("regime")
            or risk_state.get("market_regime")
            or risk_state.get("regime")
            or features.get("market_regime")
            or features.get("regime")
        )

    @staticmethod
    def _row_strategy_id(row: dict[str, Any]) -> str | None:
        metadata = ContinualLearningWorker._json_object(row.get("metadata_json"))
        features = ContinualLearningWorker._json_object(row.get("features_json"))
        value = (
            metadata.get("strategy_id")
            or metadata.get("strategy")
            or features.get("strategy_id")
            or row.get("strategy_id")
            or row.get("strategy_version")
        )
        return str(value).strip() if value is not None and str(value).strip() else None

    def run_once(self) -> int:
        if not hasattr(self, "_reward_calibrator"):
            self._reward_calibrator = RegimeRewardCalibrator()
        rows = self._rows()
        processed_now = 0
        for row in rows:
            experience_id = str(row["experience_id"])
            timestamp = row["timestamp"]
            if isinstance(timestamp, datetime):
                cursor_timestamp = timestamp.astimezone(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S.%f"
                )[:23]
            else:
                cursor_timestamp = str(timestamp)
            if experience_id in self._legacy_processed:
                self._legacy_processed.discard(experience_id)
                self._cursor_timestamp = cursor_timestamp
                self._cursor_experience_id = experience_id
                self._processed += 1
                self._save_state()
                continue
            features = self._numeric_features(row.get("features_json"))
            reward_r_multiple = self._reward_r_multiple(row)
            if features and reward_r_multiple is not None:
                apply = getattr(self.scorer, "update_experience_and_persist", None)
                if callable(apply):
                    regime = self._row_regime(row)
                    calibrated_reward = self._reward_calibrator.calibrate(
                        regime,
                        reward_r_multiple,
                        strategy_id=self._row_strategy_id(row),
                    )
                    regime_scorer = self._scorer_for_regime(regime)
                    regime_apply = getattr(
                        regime_scorer, "update_experience_and_persist", apply
                    )
                    applied = regime_apply(
                        experience_id, str(row["symbol"]), features, calibrated_reward
                    )
                    if applied:
                        processed_now += 1
                else:
                    # Compatibility for injected test scorers; production scorer
                    # always provides durable experience-level idempotency.
                    self.scorer.update_and_persist(
                        str(row["symbol"]), features, reward_r_multiple
                    )
                    processed_now += 1
            # Invalid/empty feature rows are deliberately quarantined by advancing
            # the cursor; otherwise a single poison row blocks all later batches.
            self._cursor_timestamp = cursor_timestamp
            self._cursor_experience_id = experience_id
            self._processed += 1
            self._save_state()
        logger.info(
            "learning batch completed",
            extra={
                "aitos_extra": {
                    "fetched": len(rows),
                    "processed_now": processed_now,
                    "cursor_timestamp": self._cursor_timestamp,
                    "cursor_experience_id": self._cursor_experience_id,
                    "processed_count": self._processed,
                    "samples_seen": self.scorer.n_samples_seen
                    + sum(
                        s.n_samples_seen
                        for s in getattr(self, "_regime_scorers", {}).values()
                    ),
                }
            },
        )
        return self._processed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.client.close()
        finally:
            self._release_singleton_lock()

    def request_stop(self) -> None:
        """Ask the background loop to stop after its current batch completes."""
        self._stop.set()

    def shutdown(self) -> None:
        """Persist final cursor state and release resources after the loop exits."""
        self.request_stop()
        try:
            self._save_state()
        finally:
            self.close()

    def run_forever(self) -> None:
        logger.info("learning worker loop started")
        try:
            while not self._stop.is_set():
                self.run_once()
                self._stop.wait(self.poll_seconds)
        finally:
            self.close()
