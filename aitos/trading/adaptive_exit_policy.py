"""Deterministic, position-local adaptive exit safeguards.

This policy supplements (never replaces) thesis/structure Exit Intelligence.
Initial R is immutable and stops are monotonic: this module can only tighten
protection. Stagnation counts persisted position-manager observations,
not wall-clock time.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from aitos.models.trade import Trade, TradeSide


@dataclass(frozen=True)
class AdaptiveExitResult:
    stop_price: float | None = None
    stagnation_exit: bool = False
    bars_observed: int = 0
    progress_r: float = 0.0
    reason: str = "no_change"


class AdaptiveExitPolicy:
    """Break-even ratchet and stagnation guard using immutable entry risk."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        self._be_trigger_r = float(cfg.get("breakeven_trigger_r", 0.8))
        self._be_lock_r = float(cfg.get("breakeven_lock_r", 0.15))
        self._fee_rate = float(cfg.get("round_trip_cost_rate", 0.0008))
        self._slippage_rate = float(cfg.get("slippage_buffer_rate", 0.0002))
        self._stagnation_bars = int(cfg.get("stagnation_bars", 35))
        self._stagnation_min_progress_r = float(
            cfg.get("stagnation_min_progress_r", 0.4)
        )
        self._stagnation_entry_band_pct = float(
            cfg.get("stagnation_entry_band_pct", 0.002)
        )
        if not (0 < self._be_trigger_r <= 10 and 0 <= self._be_lock_r <= 5):
            raise ValueError("invalid break-even R thresholds")
        if not (0 <= self._fee_rate <= 0.1 and 0 <= self._slippage_rate <= 0.1):
            raise ValueError("invalid transaction-cost buffers")
        if not (1 <= self._stagnation_bars <= 10000):
            raise ValueError("stagnation_bars must be between 1 and 10000")
        if not (0 <= self._stagnation_min_progress_r <= 10):
            raise ValueError("invalid stagnation progress threshold")
        if not (0 <= self._stagnation_entry_band_pct <= 0.05):
            raise ValueError("invalid stagnation entry-price band")

    def reset_trade(self, trade_id: str) -> None:
        # Observation state lives on the Trade record and survives serialization.
        return None

    def evaluate(
        self,
        trade: Trade,
        current_price: float,
        *,
        completed_bar_id: str | None = None,
    ) -> AdaptiveExitResult:
        if not isfinite(current_price) or current_price <= 0:
            return AdaptiveExitResult(reason="invalid_market_price")
        risk = float(trade.r_distance or 0.0)
        if not isfinite(risk) or risk <= 0 or trade.entry_price <= 0:
            return AdaptiveExitResult(reason="invalid_initial_risk")

        trade.record_excursion(current_price)
        sign = 1.0 if trade.side == TradeSide.LONG else -1.0
        progress_r = ((current_price - trade.entry_price) * sign) / risk
        # Position evaluation may run on every tick. Only a caller-supplied,
        # completed candle identity may advance the stagnation clock.
        count = max(0, int(getattr(trade, "adaptive_exit_observations", 0)))
        bar_id = str(completed_bar_id).strip() if completed_bar_id is not None else ""
        if bar_id and bar_id != getattr(trade, "adaptive_exit_last_bar_id", None):
            count += 1
            trade.adaptive_exit_observations = count
            trade.adaptive_exit_last_bar_id = bar_id

        candidate_stop = None
        reason = "no_change"
        if progress_r + 1e-9 >= self._be_trigger_r:
            cost_distance = trade.entry_price * (self._fee_rate + self._slippage_rate)
            lock_distance = max(self._be_lock_r * risk, cost_distance)
            candidate_stop = trade.entry_price + sign * lock_distance
            improves = (
                candidate_stop > float(trade.sl_price)
                if sign > 0
                else candidate_stop < float(trade.sl_price)
            )
            if improves:
                reason = "breakeven_ratchet"
            else:
                candidate_stop = None

        # A trade that already made the required progress must not be
        # classified as stagnant merely because it later retraced.
        best_progress_r = max(progress_r, float(trade.mfe_r or 0.0))
        max_excursion_price = max(
            float(trade.mae_price or 0.0), float(trade.mfe_price or 0.0)
        )
        max_excursion_pct = max_excursion_price / float(trade.entry_price)
        current_deviation_pct = abs(current_price - float(trade.entry_price)) / float(
            trade.entry_price
        )
        remained_near_entry = (
            max_excursion_pct <= self._stagnation_entry_band_pct
            and current_deviation_pct <= self._stagnation_entry_band_pct
        )
        stagnation = (
            bool(bar_id)
            and count >= self._stagnation_bars
            and best_progress_r < self._stagnation_min_progress_r
            and remained_near_entry
        )
        if stagnation:
            reason = "time_decay_scratch"
        return AdaptiveExitResult(
            stop_price=candidate_stop,
            stagnation_exit=stagnation,
            bars_observed=count,
            progress_r=progress_r,
            reason=reason,
        )
