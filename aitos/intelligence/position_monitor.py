"""Tiered monitoring for open positions."""

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
class PositionHealthVector:
    pnl_pct: float
    stop_distance_pct: float | None
    cvd: float | None
    delta: float | None
    liquidity_risk: float | None
    volatility_risk: float | None
    regime_risk: float | None
    reference_risk: float | None
    thesis_risk: float | None
    data_freshness_seconds: float | None


@dataclass(frozen=True)
class PositionMonitorDecision:
    tier: PositionMonitorTier
    score: float
    reasons: tuple[str, ...] = ()
    priority_score: float = 0.0
    health: PositionHealthVector | None = None


class PositionMonitorController:
    """Cheap deterministic escalation gate; no sophisticated exit decisions."""

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

    @staticmethod
    def _risk_feature(features: Mapping[str, Any], *names: str) -> float | None:
        value = PositionMonitorController._feature(features, *names)
        return None if value is None else max(0.0, min(1.0, abs(value)))

    @staticmethod
    def _priority(
        *,
        tier: PositionMonitorTier,
        pnl_pct: float,
        stop_distance_pct: float | None,
        reasons: list[str],
        features: Mapping[str, Any],
        freshness: float | None,
    ) -> float:
        score = {
            PositionMonitorTier.NORMAL: 0.0,
            PositionMonitorTier.WARNING: 30.0,
            PositionMonitorTier.EXIT_CANDIDATE: 60.0,
        }[tier]
        if pnl_pct < 0:
            score += min(abs(pnl_pct) * 2.0, 10.0)
        if "stop_breached" in reasons:
            score += 40.0
        elif stop_distance_pct is not None and stop_distance_pct > 0:
            score += max(0.0, 20.0 * (1.0 - min(stop_distance_pct / 2.0, 1.0)))
        for weight, names in (
            (15.0, ("thesis_risk", "thesis_deterioration", "thesis_invalidity")),
            (15.0, ("adverse_order_flow_risk", "order_flow_risk")),
            (12.0, ("liquidity_risk", "liquidity_stress")),
            (10.0, ("volatility_risk", "volatility_stress")),
            (8.0, ("regime_risk", "market_regime_risk")),
            (8.0, ("reference_risk", "btc_relationship_risk", "lead_lag_risk")),
        ):
            score += weight * (
                PositionMonitorController._risk_feature(features, *names) or 0.0
            )
        if freshness is not None:
            score += min(max(freshness, 0.0) / 5.0, 1.0) * 10.0
        if "explicit_exit_signal" in reasons:
            score += 20.0
        return min(score, 100.0)

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
                PositionMonitorTier.WARNING, 1.0, ("invalid_price_context",), 100.0
            )

        pnl_pct = (
            ((current_price - entry) / entry * 100.0)
            if side == "LONG"
            else ((entry - current_price) / entry * 100.0)
        )
        if pnl_pct < 0:
            score += 1.0
            reasons.append("negative_pnl")

        stop_distance_pct: float | None = None
        if sl > 0:
            stop_distance_pct = abs(current_price - sl) / current_price * 100.0
            breached = (side == "LONG" and current_price <= sl) or (
                side == "SHORT" and current_price >= sl
            )
            if breached:
                score += 5.0
                reasons.append("stop_breached")
            elif stop_distance_pct <= self.exit_sl_distance_pct:
                score += 3.0
                reasons.append("near_stop_critical")
            elif stop_distance_pct <= self.warning_sl_distance_pct:
                score += 2.0
                reasons.append("near_stop")

        cvd = self._feature(features, "cvd", "cvd_value", "cumulative_delta")
        delta = self._feature(features, "delta", "trade_delta", "order_flow_delta")
        if (
            cvd is not None
            and pnl_pct > 0
            and ((side == "LONG" and cvd < 0) or (side == "SHORT" and cvd > 0))
        ):
            score += 1.5
            reasons.append("cvd_divergence")
        if delta is not None and (
            (side == "LONG" and delta < 0) or (side == "SHORT" and delta > 0)
        ):
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
        if bool(features.get("exit_candidate") or features.get("exit_signal")):
            score += 4.0
            reasons.append("explicit_exit_signal")

        raw_tier = (
            PositionMonitorTier.EXIT_CANDIDATE
            if score >= self.exit_score
            else (
                PositionMonitorTier.WARNING
                if score >= self.warning_score
                else PositionMonitorTier.NORMAL
            )
        )

        # Hysteresis is an explicit state machine: a position must produce
        # `hysteresis_updates` consecutive NORMAL observations before a prior
        # WARNING/EXIT_CANDIDATE state is allowed to clear. Any renewed risk
        # observation immediately resets the clear streak.
        trade_id = str(getattr(trade, "trade_id", ""))
        previous_tier = self._last_tier.get(trade_id, PositionMonitorTier.NORMAL)
        clear_count = self._clear_count.get(trade_id, 0)
        if raw_tier == PositionMonitorTier.NORMAL and previous_tier in {
            PositionMonitorTier.WARNING,
            PositionMonitorTier.EXIT_CANDIDATE,
        }:
            clear_count += 1
            if clear_count >= self.hysteresis_updates:
                stable_tier = PositionMonitorTier.NORMAL
                self._clear_count.pop(trade_id, None)
            else:
                stable_tier = previous_tier
                self._clear_count[trade_id] = clear_count
        else:
            stable_tier = raw_tier
            self._clear_count.pop(trade_id, None)
        self._last_tier[trade_id] = stable_tier

        health = PositionHealthVector(
            pnl_pct=pnl_pct,
            stop_distance_pct=stop_distance_pct,
            cvd=cvd,
            delta=delta,
            liquidity_risk=self._risk_feature(
                features, "liquidity_risk", "liquidity_stress"
            ),
            volatility_risk=self._risk_feature(
                features, "volatility_risk", "volatility_stress"
            ),
            regime_risk=self._risk_feature(
                features, "regime_risk", "market_regime_risk"
            ),
            reference_risk=self._risk_feature(
                features, "reference_risk", "btc_relationship_risk", "lead_lag_risk"
            ),
            thesis_risk=self._risk_feature(
                features, "thesis_risk", "thesis_deterioration", "thesis_invalidity"
            ),
            data_freshness_seconds=freshness,
        )
        priority = self._priority(
            tier=stable_tier,
            pnl_pct=pnl_pct,
            stop_distance_pct=stop_distance_pct,
            reasons=reasons,
            features=features,
            freshness=freshness,
        )
        return PositionMonitorDecision(
            tier=stable_tier,
            score=score,
            reasons=tuple(reasons),
            priority_score=priority,
            health=health,
        )

    def clear(self, trade_id: str) -> None:
        self._last_tier.pop(trade_id, None)
        self._clear_count.pop(trade_id, None)
