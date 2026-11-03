"""Durable continual-learning worker for historical backtest experiences."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import clickhouse_connect

from aitos.config.paths import get_model_data_dir, resolve_project_path
from aitos.intelligence.deep_rl_policy import DeepValueRLScorer

logger = logging.getLogger(__name__)


class ContinualLearningWorker:
    """Poll ClickHouse and incrementally learn from persisted backtests."""

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
        lookback_hours: int = 168,
        batch_limit: int = 5000,
        poll_seconds: int = 60,
    ) -> None:
        self._stop = False
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
        state_path = str(state_file)
        model_path = str(model_file)
        # Ensure model directories exist before any learning/persistence
        for p in (state_path, model_path):
            Path(p).parent.mkdir(parents=True, exist_ok=True)
        logger.info(
            "learning worker initializing",
            extra={
                "aitos_extra": {
                    "database": database,
                    "batch_limit": batch_limit,
                    "poll_seconds": poll_seconds,
                    "lookback_hours": lookback_hours,
                    "model_path": model_path,
                }
            },
        )
        self.client = clickhouse_connect.get_client(
            host=host, port=port, username=user, password=password, database=database
        )
        self.database = database
        self.state_path = Path(state_path)
        self.batch_limit = batch_limit
        self.poll_seconds = poll_seconds
        self.lookback = timedelta(hours=lookback_hours)
        self.scorer = DeepValueRLScorer(state_path=model_path)
        self.scorer.load_state(model_path)
        self._processed: set[str] = set()
        self._load_state()
        logger.info(
            "learning worker initialized",
            extra={
                "aitos_extra": {
                    "processed_experiences": len(self._processed),
                    "samples_seen": self.scorer.n_samples_seen,
                }
            },
        )

    def close(self) -> None:
        self.client.close()
        logger.info("learning worker closed")

    def shutdown(self) -> None:
        """Signal the worker to stop after the current iteration, then clean up."""
        self._stop = True
        self._save_state()
        self.close()
        logger.info("learning worker shut down gracefully")

    def run_forever(self) -> None:
        logger.info("learning worker loop started")
        try:
            while not self._stop:
                self.run_once()
                for _ in range(self.poll_seconds):
                    if self._stop:
                        break
                    time.sleep(1)
        finally:
            self.close()

    def _load_state(self) -> None:
        if not self.state_path.exists():
            logger.info(
                "learning state file not found; starting fresh",
                extra={"aitos_extra": {"state_path": str(self.state_path)}},
            )
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._processed = set(str(x) for x in data.get("processed_experiences", []))
            logger.info(
                "learning state loaded",
                extra={"aitos_extra": {"processed_experiences": len(self._processed)}},
            )
        except (OSError, ValueError, TypeError):
            self._processed = set()
            logger.exception(
                "learning state load failed",
                extra={"aitos_extra": {"state_path": str(self.state_path)}},
            )

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "processed_experiences": sorted(self._processed)[-10000:],
            "n_samples_seen": self.scorer.n_samples_seen,
        }
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)
        logger.info(
            "learning state persisted",
            extra={
                "aitos_extra": {
                    "processed_experiences": len(self._processed),
                    "samples_seen": self.scorer.n_samples_seen,
                }
            },
        )

    def _rows(self) -> list[dict[str, Any]]:
        start = datetime.now(timezone.utc) - self.lookback
        sql = """
        SELECT experience_id, source, symbol, decision, outcome, reward,
               features_json, risk_state_json, metadata_json
        FROM learning_experiences
        WHERE source = 'backtest'
          AND decision = 'backtest_trade' AND outcome = 'closed'
          AND timestamp >= {start:DateTime64(3)}
        ORDER BY timestamp ASC
        LIMIT {limit:UInt32}
        """
        try:
            result = self.client.query(
                sql, parameters={"start": start, "limit": self.batch_limit}
            )
            rows = [dict(zip(result.column_names, row)) for row in result.result_rows]
            logger.info(
                "learning batch fetched",
                extra={
                    "aitos_extra": {
                        "rows": len(rows),
                        "lookback_hours": self.lookback.total_seconds() / 3600,
                    }
                },
            )
            return rows
        except Exception:
            logger.exception("learning batch query failed")
            raise

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
        result: dict[str, float] = {}
        for key, raw in value.items():
            if str(key) in excluded or not isinstance(raw, (int, float, bool)):
                continue
            number = float(raw)
            if number == number and abs(number) != float("inf"):
                result[str(key)] = number
        if result:
            # Keep training aligned with inference; never feed the model its own score.
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
        """Return a strictly validated R reward; reject summaries and unscaled PnL."""
        if row.get("decision") != "backtest_trade" or row.get("outcome") != "closed":
            return None
        try:
            reward = float(row.get("reward"))
        except (TypeError, ValueError):
            return None
        if not (reward == reward and abs(reward) != float("inf")):
            return None

        metadata = cls._json_object(row.get("metadata_json"))
        risk_state = cls._json_object(row.get("risk_state_json"))
        features = cls._json_object(row.get("features_json"))
        reward_unit = str(metadata.get("reward_unit", "")).strip().lower()
        if reward_unit in {"r", "r_multiple", "risk_multiple"}:
            return reward
        if reward_unit not in {"", "quote_pnl", "quote_currency", "usd"}:
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
        if not (risk_amount > 0 and risk_amount == risk_amount and abs(risk_amount) != float("inf")):
            return None
        normalized = reward / risk_amount
        return normalized if normalized == normalized and abs(normalized) != float("inf") else None

    def run_once(self) -> int:
        rows = self._rows()
        changed = False
        processed_now = 0
        skipped = 0
        for row in rows:
            experience_id = str(row["experience_id"])
            if experience_id in self._processed:
                skipped += 1
                continue
            features = self._numeric_features(row.get("features_json"))
            reward_r_multiple = self._reward_r_multiple(row)
            if not features or reward_r_multiple is None:
                skipped += 1
                # Permanently ineligible rows (e.g. summary or unscaled PnL)
                # must not pin the bounded lookback query at the same records.
                self._processed.add(experience_id)
                changed = True
                continue
            try:
                self.scorer.update_and_persist(
                    str(row["symbol"]), features, reward_r_multiple
                )
            except Exception:
                logger.exception(
                    "learning update failed",
                    extra={
                        "aitos_extra": {
                            "experience_id": experience_id,
                            "symbol": str(row["symbol"]),
                        }
                    },
                )
                raise
            self._processed.add(experience_id)
            changed = True
            processed_now += 1
        if changed:
            self._save_state()
        logger.info(
            "learning batch completed",
            extra={
                "aitos_extra": {
                    "fetched": len(rows),
                    "processed_now": processed_now,
                    "skipped": skipped,
                    "total_processed": len(self._processed),
                    "samples_seen": self.scorer.n_samples_seen,
                }
            },
        )
        return len(self._processed)
