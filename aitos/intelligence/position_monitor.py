"""Tiered monitoring for open positions.

Every open position remains continuously monitored with cheap signals. Only
positions showing deterioration, risk proximity, or an explicit exit trigger
are escalated into progressively more expensive analysis.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class PositionMonitorTier(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    EXIT_CANDIDATE = "exit_candidate"


@dataclass(frozen=True)
class PositionMonitorDecision:
    tier: PositionMonitorTier
    score: float
    reasons: tuple[str, ...] = ()


class PositionMonitorController:
    """Cheap, deterministic escalation gate used before expensive analysis.

    The controller deliberately has no exchange or database dependencies. It
    can therefore run for every open position on every live price update.
    Feature names are intentionally tolerant because different data providers
    expose CVD/delta/volatility/freshness under slightly different names.
    """

    def __init__(
        self,
        *,
        warning_score: float = 2.0,
        exit_score: float = 4.0,
        warning_sl_distance_pct: float = 1.0,
        exit_sl_distance_pct: float = 0.25,
        hysteresis_updates: int = 3,
    ) -> None:
        self.warning_score = warning_score
        self.exit_score = exit_score
        self.warning_sl_distance_pct = warning_sl_distance_pct
        self.exit_sl_distance_pct = exit_sl_distance_pct
        self.hysteresis_updates = max(1, hysteresis_updates)
        self._last_tier: dict[str, PositionMonitorTier] = {}
        self._clear_count: dict[str, int] = {}

    @staticmethod
    def _feature(features: Mapping[str, Any], *names: str) -> float | None:
        for name in names:
            value = features.get(name)
            try:
                if value is not None:
                    return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def evaluate(
        self,
        *,
        trade: Any,
        current_price: float,
        extra_features: Mapping[str, Any] | None = None,
    ) -> PositionMonitorDecision:
        features = extra_features or {}
        reasons: list[str] = []
        score = 0.0

        entry = float(getattr(trade, "entry_price", 0.0) or 0.0)
        sl = float(getattr(trade, "sl_price", 0.0) or 0.0)
        side = str(
            getattr(getattr(trade, "side", None), "value", getattr(trade, "side", ""))
        ).upper()
        if current_price <= 0 or entry <= 0:
            return PositionMonitorDecision(
                PositionMonitorTier.WARNING, 1.0, ("invalid_price_context",)
            )

        pnl_pct = (
            ((current_price - entry) / entry * 100.0)
            if side == "LONG"
            else ((entry - current_price) / entry * 100.0)
        )
        if pnl_pct < 0:
            score += 1.0
            reasons.append("negative_pnl")

        if sl > 0:
            sl_distance_pct = abs(current_price - sl) / current_price * 100.0
            if (side == "LONG" and current_price <= sl) or (
                side == "SHORT" and current_price >= sl
            ):
                score += 5.0
                reasons.append("stop_breached")
            elif sl_distance_pct <= self.exit_sl_distance_pct:
                score += 3.0
                reasons.append("near_stop_critical")
            elif sl_distance_pct <= self.warning_sl_distance_pct:
                score += 2.0
                reasons.append("near_stop")

        cvd = self._feature(features, "cvd", "cvd_value", "cumulative_delta")
        delta = self._feature(features, "delta", "trade_delta", "order_flow_delta")
        if cvd is not None and pnl_pct > 0:
            if (side == "LONG" and cvd < 0) or (side == "SHORT" and cvd > 0):
                score += 1.5
                reasons.append("cvd_divergence")
        if delta is not None:
            if (side == "LONG" and delta < 0) or (side == "SHORT" and delta > 0):
                score += 1.0
                reasons.append("adverse_delta")

        volatility = self._feature(features, "volatility_pct", "atr_pct", "volatility")
        if volatility is not None and volatility > 3.0:
            score += 1.0
            reasons.append("volatility_expansion")

        freshness = self._feature(
            features, "data_freshness_seconds", "freshness_seconds"
        )
        if freshness is not None and freshness > 5.0:
            score += 2.0
            reasons.append("stale_market_data")

        explicit_exit = bool(
            features.get("exit_candidate") or features.get("exit_signal")
        )
        if explicit_exit:
            score += 4.0
            reasons.append("explicit_exit_signal")

        if score >= self.exit_score:
            tier = PositionMonitorTier.EXIT_CANDIDATE
            self._clear_count.pop(str(getattr(trade, "trade_id", "")), None)
        elif score >= self.warning_score:
            tier = PositionMonitorTier.WARNING
            self._clear_count.pop(str(getattr(trade, "trade_id", "")), None)
        else:
            tier = PositionMonitorTier.NORMAL

        trade_id = str(getattr(trade, "trade_id", ""))
        previous = self._last_tier.get(trade_id)
        if (
            previous
            in {PositionMonitorTier.WARNING, PositionMonitorTier.EXIT_CANDIDATE}
            and tier == PositionMonitorTier.NORMAL
        ):
            count = self._clear_count.get(trade_id, 0) + 1
            if count < self.hysteresis_updates:
                tier = previous
            else:
                self._clear_count.pop(trade_id, None)
        else:
            self._clear_count.pop(trade_id, None)
        self._last_tier[trade_id] = tier
        return PositionMonitorDecision(tier=tier, score=score, reasons=tuple(reasons))

    def clear(self, trade_id: str) -> None:
        self._last_tier.pop(trade_id, None)
        self._clear_count.pop(trade_id, None)
