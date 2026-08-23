"""Durable continual-learning worker for historical backtest experiences.

Paper/live trades update the same persistent neural scorer through the event-driven
feedback loop. The scorer serializes read-modify-write updates so historical and
online learning cannot overwrite one another.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import clickhouse_connect

from aitos.intelligence.deep_rl_policy import DeepValueRLScorer


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
        state_path: str = "models/online_rl/worker_state.json",
        model_path: str = "models/online_rl/deep_value.pkl",
        lookback_hours: int = 168,
        batch_limit: int = 5000,
        poll_seconds: int = 60,
    ) -> None:
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

    def close(self) -> None:
        self.client.close()

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._processed = set(str(x) for x in data.get("processed_experiences", []))
        except (OSError, ValueError, TypeError):
            self._processed = set()

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

    def _rows(self) -> list[dict[str, Any]]:
        start = datetime.now(timezone.utc) - self.lookback
        sql = f"""
        SELECT experience_id, source, symbol, outcome, reward, features_json
        FROM {self.database}.learning_experiences
        WHERE source = 'backtest' AND outcome IS NOT NULL
          AND timestamp >= {{start:DateTime64(3)}}
        ORDER BY timestamp ASC
        LIMIT {{limit:UInt32}}
        """
        result = self.client.query(
            sql, parameters={"start": start, "limit": self.batch_limit}
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
        return {
            str(key): float(raw)
            for key, raw in value.items()
            if isinstance(raw, (int, float, bool))
        }

    def run_once(self) -> int:
        rows = self._rows()
        changed = False
        for row in rows:
            experience_id = str(row["experience_id"])
            if experience_id in self._processed:
                continue
            features = self._numeric_features(row["features_json"])
            if not features:
                continue
            self.scorer.update_and_persist(
                str(row["symbol"]), features, float(row["reward"] or 0.0)
            )
            self._processed.add(experience_id)
            changed = True
        if changed:
            self._save_state()
        return len(self._processed)

    def run_forever(self) -> None:
        try:
            while True:
                self.run_once()
                time.sleep(self.poll_seconds)
        finally:
            self.close()
