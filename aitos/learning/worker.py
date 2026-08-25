"""Durable continual-learning worker for historical backtest experiences."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import clickhouse_connect

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
        state_path: str = "/models/online_rl/worker_state.json",
        model_path: str = "/models/online_rl/deep_value.pkl",
        lookback_hours: int = 168,
        batch_limit: int = 5000,
        poll_seconds: int = 60,
    ) -> None:
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
        SELECT experience_id, source, symbol, outcome, reward, features_json
        FROM learning_experiences
        WHERE source = 'backtest' AND outcome IS NOT NULL
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
        return {
            str(key): float(raw)
            for key, raw in value.items()
            if isinstance(raw, (int, float, bool))
        }

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
            features = self._numeric_features(row["features_json"])
            if not features:
                skipped += 1
                continue
            try:
                self.scorer.update_and_persist(
                    str(row["symbol"]), features, float(row["reward"] or 0.0)
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

    def run_forever(self) -> None:
        logger.info("learning worker loop started")
        try:
            while True:
                self.run_once()
                time.sleep(self.poll_seconds)
        finally:
            self.close()
