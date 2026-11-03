"""Persistent rejection markouts and ranking stability for opportunity management."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class CandidateOutcomeStore:
    """Persist rejected opportunities and forward markouts without blocking market data."""

    HORIZONS_SECONDS = (60, 300, 900)

    def __init__(self, path: str | Path, max_pending: int = 20000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_pending = max(100, int(max_pending))
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS candidate_rejections (
                rejection_id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                score REAL NOT NULL,
                reason TEXT NOT NULL,
                features_json TEXT NOT NULL,
                horizon_mask INTEGER NOT NULL DEFAULT 0,
                best_return_pct REAL NOT NULL DEFAULT 0,
                worst_return_pct REAL NOT NULL DEFAULT 0,
                last_price REAL,
                last_update_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_candidate_rejections_pending
            ON candidate_rejections(symbol, created_at, horizon_mask);
            CREATE TABLE IF NOT EXISTS candidate_markouts (
                rejection_id TEXT NOT NULL,
                horizon_seconds INTEGER NOT NULL,
                observed_at REAL NOT NULL,
                price REAL NOT NULL,
                signed_return_pct REAL NOT NULL,
                mae_pct REAL NOT NULL,
                mfe_pct REAL NOT NULL,
                PRIMARY KEY(rejection_id, horizon_seconds)
            );
            """
        )
        self._conn.commit()

    @staticmethod
    def _epoch(value: Any = None) -> float:
        if value is None:
            return datetime.now(timezone.utc).timestamp()
        if isinstance(value, datetime):
            return value.timestamp()
        if isinstance(value, (int, float)):
            return float(value)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    def record_rejection(
        self, candidate: Any, reason: str, timestamp: Any = None
    ) -> str:
        entry = float(candidate.entry_price)
        if entry <= 0:
            return ""
        rejection_id = str(uuid.uuid4())
        side = str(candidate.direction.value).upper()
        features = {
            "component_scores": dict(candidate.component_scores),
            "component_availability": dict(candidate.component_availability),
            "regime": candidate.regime,
            "rationale": list(candidate.rationale),
        }
        with self._lock:
            self._conn.execute(
                """INSERT INTO candidate_rejections
                (rejection_id, created_at, symbol, side, entry_price, score, reason, features_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rejection_id,
                    self._epoch(timestamp),
                    candidate.symbol.upper(),
                    side,
                    entry,
                    float(candidate.composite_score),
                    str(reason),
                    json.dumps(features, sort_keys=True, default=str),
                ),
            )
            self._conn.execute(
                """DELETE FROM candidate_rejections WHERE rejection_id IN (
                    SELECT rejection_id FROM candidate_rejections
                    ORDER BY created_at DESC LIMIT -1 OFFSET ?
                )""",
                (self.max_pending,),
            )
            self._conn.commit()
        return rejection_id

    def update_price(self, symbol: str, price: float, timestamp: Any = None) -> int:
        price = float(price)
        if price <= 0:
            return 0
        now = self._epoch(timestamp)
        updated = 0
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM candidate_rejections
                WHERE symbol = ? AND horizon_mask < 7 AND created_at <= ?
                ORDER BY created_at DESC LIMIT ?""",
                (symbol.upper(), now, self.max_pending),
            ).fetchall()
            if not rows:
                return 0
            for row in rows:
                signed = (
                    (price / row["entry_price"] - 1.0)
                    * (1.0 if row["side"] == "LONG" else -1.0)
                    * 100.0
                )
                best = max(float(row["best_return_pct"]), signed)
                worst = min(float(row["worst_return_pct"]), signed)
                mask = int(row["horizon_mask"])
                self._conn.execute(
                    """UPDATE candidate_rejections SET best_return_pct = ?,
                    worst_return_pct = ?, last_price = ?, last_update_at = ?
                    WHERE rejection_id = ?""",
                    (best, worst, price, now, row["rejection_id"]),
                )
                age = max(0.0, now - float(row["created_at"]))
                for bit, horizon in enumerate(self.HORIZONS_SECONDS):
                    flag = 1 << bit
                    if age >= horizon and not (mask & flag):
                        self._conn.execute(
                            """INSERT OR IGNORE INTO candidate_markouts
                            (rejection_id, horizon_seconds, observed_at, price,
                            signed_return_pct, mae_pct, mfe_pct)
                            VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (
                                row["rejection_id"],
                                horizon,
                                now,
                                price,
                                signed,
                                max(0.0, -worst),
                                max(0.0, best),
                            ),
                        )
                        mask |= flag
                        updated += 1
                self._conn.execute(
                    "UPDATE candidate_rejections SET horizon_mask = ? WHERE rejection_id = ?",
                    (mask, row["rejection_id"]),
                )
            self._conn.commit()
        return updated

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            pending = self._conn.execute(
                "SELECT COUNT(*) FROM candidate_rejections WHERE horizon_mask < 7"
            ).fetchone()[0]
            rejected = self._conn.execute(
                "SELECT COUNT(*) FROM candidate_rejections"
            ).fetchone()[0]
            markouts = self._conn.execute(
                "SELECT COUNT(*) FROM candidate_markouts"
            ).fetchone()[0]
            rows = self._conn.execute(
                """SELECT m.horizon_seconds, COUNT(*) AS samples,
                AVG(m.signed_return_pct) AS mean_signed_return_pct,
                AVG(m.mae_pct) AS mean_mae_pct, AVG(m.mfe_pct) AS mean_mfe_pct,
                SUM(CASE WHEN m.signed_return_pct > 0.1 THEN 1 ELSE 0 END) AS missed,
                SUM(CASE WHEN m.signed_return_pct < -0.1 THEN 1 ELSE 0 END) AS correctly_rejected
                FROM candidate_markouts m GROUP BY m.horizon_seconds
                ORDER BY m.horizon_seconds"""
            ).fetchall()
        horizon_summary = {}
        for row in rows:
            samples = int(row["samples"])
            horizon_summary[str(row["horizon_seconds"])] = {
                "samples": samples,
                "mean_signed_return_pct": round(
                    float(row["mean_signed_return_pct"] or 0), 6
                ),
                "mean_mae_pct": round(float(row["mean_mae_pct"] or 0), 6),
                "mean_mfe_pct": round(float(row["mean_mfe_pct"] or 0), 6),
                "missed_opportunity_rate": round(float(row["missed"] or 0) / samples, 4),
                "correct_rejection_rate": round(
                    float(row["correctly_rejected"] or 0) / samples, 4
                ),
            }
        return {
            "rejections_persisted": rejected,
            "pending_markouts": pending,
            "markouts_recorded": markouts,
            "horizon_summary": horizon_summary,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class RankingHysteresis:
    """Retain a small incumbent-order advantage; raw scores remain untouched."""

    def __init__(self, margin_points: float = 1.0, max_symbols: int = 5000) -> None:
        self.margin_points = max(0.0, float(margin_points))
        self.max_symbols = max(100, int(max_symbols))
        self._previous_order: dict[str, int] = {}
        self._cycles = 0
        self._retained = 0
        self._challengers = 0

    def rank(self, candidates: list[Any], top_n: int) -> list[Any]:
        if not candidates:
            self._cycles += 1
            return []
        previous = self._previous_order
        # The bonus is only an ordering tie-breaker. It never changes the
        # candidate's composite_score, confidence, threshold or risk inputs.
        def key(candidate: Any):
            symbol = candidate.symbol.upper()
            rank = previous.get(symbol)
            bonus = 0.0
            if rank is not None and rank < max(1, top_n):
                bonus = self.margin_points * (1.0 - rank / max(1, top_n))
            return (
                float(candidate.composite_score) + bonus,
                float(candidate.composite_score),
            )

        ordered = sorted(
            (c for c in candidates if c.composite_score >= 0),
            key=key,
            reverse=True,
        )
        self._retained += sum(
            1 for c in ordered[:top_n] if c.symbol.upper() in previous
        )
        self._challengers += sum(
            1 for c in ordered[:top_n] if c.symbol.upper() not in previous
        )
        for index, candidate in enumerate(ordered[: self.max_symbols]):
            self._previous_order[candidate.symbol.upper()] = index
        while len(self._previous_order) > self.max_symbols:
            self._previous_order.pop(next(iter(self._previous_order)))
        self._cycles += 1
        return ordered[:top_n]

    def snapshot(self) -> dict[str, int | float]:
        return {
            "ranking_cycles": self._cycles,
            "incumbents_retained": self._retained,
            "new_challengers_selected": self._challengers,
            "hysteresis_margin_points": self.margin_points,
            "symbols_in_previous_ranking": len(self._previous_order),
        }
