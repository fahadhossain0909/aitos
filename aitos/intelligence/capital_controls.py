"""Secondary capital-safety controls used at the final deployment boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite


@dataclass(frozen=True)
class CapitalControlConfig:
    """Conservative defaults; calibrate from paper/live telemetry."""

    opportunity_max_age_seconds: float = 30.0
    max_daily_loss_pct: float = 3.0
    max_consecutive_losses: int = 5
    min_liquidity_for_execution: float = 4.0
    adverse_slippage_multiplier: float = 2.0
    calibration_min_samples: int = 50


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
    """Online reliability calibration for probability-like model outputs."""

    def __init__(self, config: CapitalControlConfig | None = None) -> None:
        self.config = config or CapitalControlConfig()
        self._bins: dict[int, list[int]] = {}

    @staticmethod
    def _bin(probability: float) -> int:
        return max(0, min(9, int(max(0.0, min(0.999999, probability)) * 10)))

    def observe(self, predicted_loss_probability: float, realized_loss: bool) -> None:
        key = self._bin(float(predicted_loss_probability))
        bucket = self._bins.setdefault(key, [0, 0])
        bucket[0] += 1
        bucket[1] += int(bool(realized_loss))

    @property
    def samples(self) -> int:
        return sum(bucket[0] for bucket in self._bins.values())

    def calibrate(self, probability: float) -> float:
        raw = max(0.0, min(1.0, float(probability)))
        if self.samples < self.config.calibration_min_samples:
            return raw
        bucket = self._bins.get(self._bin(raw))
        if not bucket or bucket[0] == 0:
            return raw
        empirical = bucket[1] / bucket[0]
        return max(0.0, min(1.0, empirical))
