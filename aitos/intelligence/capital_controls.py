"""Secondary capital-safety controls used at the final deployment boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
import sqlite3
from threading import RLock


@dataclass(frozen=True)
class CapitalControlConfig:
    """Conservative defaults; calibrate from paper/live telemetry."""

    opportunity_max_age_seconds: float = 30.0
    opportunity_decay_half_life_seconds: float = 15.0
    max_daily_loss_pct: float = 3.0
    max_consecutive_losses: int = 5
    risk_reduction_after_losses: int = 2
    risk_reduction_multiplier: float = 0.5
    min_liquidity_for_execution: float = 4.0
    adverse_slippage_multiplier: float = 2.0
    calibration_min_samples: int = 50

    def __post_init__(self) -> None:
        if (
            not isfinite(self.opportunity_max_age_seconds)
            or self.opportunity_max_age_seconds <= 0
        ):
            raise ValueError("opportunity_max_age_seconds must be positive")
        if (
            not isfinite(self.opportunity_decay_half_life_seconds)
            or self.opportunity_decay_half_life_seconds <= 0
        ):
            raise ValueError("opportunity_decay_half_life_seconds must be positive")
        if self.risk_reduction_after_losses < 1:
            raise ValueError("risk_reduction_after_losses must be >= 1")
        if not isfinite(self.risk_reduction_multiplier) or not (
            0.0 < self.risk_reduction_multiplier <= 1.0
        ):
            raise ValueError("risk_reduction_multiplier must be in (0, 1]")


class CapitalCircuitBreaker:
    """Hard stop for loss streaks and daily loss, independent of strategy score."""

    def __init__(self, config: CapitalControlConfig | None = None) -> None:
        self.config = config or CapitalControlConfig()

    def check(
        self, *, daily_pnl_pct: float = 0.0, consecutive_losses: int = 0
    ) -> tuple[bool, str]:
        if not isfinite(float(daily_pnl_pct)):
            return False, "invalid_daily_pnl"
        if float(daily_pnl_pct) <= -self.config.max_daily_loss_pct:
            return False, "daily_loss_circuit_breaker"
        if int(consecutive_losses) >= self.config.max_consecutive_losses:
            return False, "consecutive_loss_circuit_breaker"
        return True, "approved"


def opportunity_age_seconds(detected_at: str, *, now: datetime | None = None) -> float:
    """Return age of an opportunity; invalid timestamps are treated as stale."""
    try:
        parsed = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return max(0.0, (current - parsed).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return float("inf")


def execution_cost_bps(
    *,
    base_fee_bps: float,
    base_slippage_bps: float,
    liquidity_score: float,
    volatility_score: float | None,
    config: CapitalControlConfig | None = None,
) -> float:
    """Estimate execution friction conservatively before capital authorization."""
    cfg = config or CapitalControlConfig()
    liquidity = max(0.0, min(10.0, float(liquidity_score)))
    liquidity_multiplier = (
        1.0
        if liquidity >= cfg.min_liquidity_for_execution
        else cfg.adverse_slippage_multiplier
    )
    # A missing volatility estimate still carries a small model-risk buffer;
    # zero friction must never be assumed merely because telemetry is absent.
    volatility = (
        0.01
        if volatility_score is None
        else max(0.0, min(1.0, float(volatility_score)))
    )
    fee = max(0.0, float(base_fee_bps))
    slippage = (
        max(0.0, float(base_slippage_bps)) * liquidity_multiplier * (1.0 + volatility)
    )
    return fee + slippage


class ProbabilityCalibrator:
    """Online reliability calibration with optional durable bin counts."""

    def __init__(
        self,
        config: CapitalControlConfig | None = None,
        path: str | Path | None = None,
    ) -> None:
        self.config = config or CapitalControlConfig()
        self._bins: dict[int, list[int]] = {}
        self._lock = RLock()
        self._conn: sqlite3.Connection | None = None
        if path is not None:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(target), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS probability_calibration_bins (
                    bin INTEGER PRIMARY KEY,
                    samples INTEGER NOT NULL,
                    losses INTEGER NOT NULL
                )
                """
            )
            self._conn.commit()
            for row in self._conn.execute(
                "SELECT bin, samples, losses FROM probability_calibration_bins"
            ):
                self._bins[int(row[0])] = [int(row[1]), int(row[2])]

    @staticmethod
    def _bin(probability: float) -> int:
        return max(0, min(9, int(max(0.0, min(0.999999, probability)) * 10)))

    def observe(self, predicted_loss_probability: float, realized_loss: bool) -> None:
        probability = float(predicted_loss_probability)
        if not isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError("predicted_loss_probability must be within [0, 1]")
        if not isinstance(realized_loss, bool):
            raise ValueError("realized_loss must be boolean")
        key = self._bin(probability)
        with self._lock:
            bucket = self._bins.setdefault(key, [0, 0])
            bucket[0] += 1
            bucket[1] += int(realized_loss)
            if self._conn is not None:
                self._conn.execute(
                    """
                    INSERT INTO probability_calibration_bins(bin, samples, losses)
                    VALUES (?, 1, ?)
                    ON CONFLICT(bin) DO UPDATE SET
                        samples = samples + 1,
                        losses = losses + excluded.losses
                    """,
                    (key, int(realized_loss)),
                )
                self._conn.commit()

    @property
    def samples(self) -> int:
        with self._lock:
            return sum(bucket[0] for bucket in self._bins.values())

    def calibrate(self, probability: float) -> float:
        raw = float(probability)
        if not isfinite(raw) or not 0.0 <= raw <= 1.0:
            raise ValueError("probability must be finite and within [0, 1]")
        with self._lock:
            if sum(bucket[0] for bucket in self._bins.values()) < self.config.calibration_min_samples:
                return raw
            bucket = self._bins.get(self._bin(raw))
            if not bucket or bucket[0] == 0:
                return raw
            empirical = bucket[1] / bucket[0]
            return max(0.0, min(1.0, empirical))

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

