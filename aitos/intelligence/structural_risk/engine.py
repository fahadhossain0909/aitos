"""Structural Risk Engine — thesis-invalidation stop placement.

Priority order (thesis invalidation, NOT always tightest level):

1. Explicit structure break level
2. Protected / major swing (hierarchy)
3. Volume-profile value-area boundary
4. Significant liquidity pool
5. Micro swing (only if no better candidate)
6. Fallback: volatility-aware buffer from entry
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from aitos.intelligence.amt.volume_profile import VolumeProfile
from aitos.intelligence.liquidity_tracker import LiquidityEvent
from aitos.intelligence.market_state.models import MarketState
from aitos.intelligence.structural_risk.hierarchy import (
    DEFAULT_HIERARCHY_SLACK_PCT,
    DEFAULT_MICRO_SWING_PCT,
    TYPE_RANK,
    classify_swing,
    select_by_hierarchy,
)
from aitos.intelligence.structural_risk.models import StructuralStop
from aitos.logging_setup import get_logger

logger = get_logger("aitos.intelligence.structural_risk")

DEFAULT_VOL_BUFFER_ATR_MULT = 0.15
DEFAULT_MIN_BUFFER_PCT = 0.0008
DEFAULT_MAX_STOP_PCT = 0.04
DEFAULT_FALLBACK_PCT = 0.012


class StructuralRiskEngine:
    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        self._vol_buffer_atr_mult = float(
            cfg.get("vol_buffer_atr_mult", DEFAULT_VOL_BUFFER_ATR_MULT)
        )
        self._min_buffer_pct = float(cfg.get("min_buffer_pct", DEFAULT_MIN_BUFFER_PCT))
        self._max_stop_pct = float(cfg.get("max_stop_pct", DEFAULT_MAX_STOP_PCT))
        self._fallback_pct = float(cfg.get("fallback_pct", DEFAULT_FALLBACK_PCT))
        self._micro_swing_pct = float(cfg.get("micro_swing_pct", DEFAULT_MICRO_SWING_PCT))
        self._hierarchy_slack_pct = float(
            cfg.get("hierarchy_slack_pct", DEFAULT_HIERARCHY_SLACK_PCT)
        )

    def compute(
        self,
        *,
        symbol: str,
        side: str,
        entry_price: float,
        market_state: MarketState | None = None,
        volume_profile: VolumeProfile | None = None,
        swing_lows: Sequence[float] = (),
        swing_highs: Sequence[float] = (),
        structure_break_level: float | None = None,
        liquidity_events: Sequence[LiquidityEvent] = (),
        atr: float | None = None,
        timestamp: datetime | None = None,
    ) -> StructuralStop:
        side = side.upper()
        if side not in ("LONG", "SHORT"):
            raise ValueError(f"side must be LONG or SHORT, got {side!r}")
        if entry_price <= 0:
            raise ValueError("entry_price must be > 0")

        ts = timestamp or (
            market_state.timestamp if market_state else datetime.now(timezone.utc)
        )
        notes: list[str] = []
        features: dict[str, float] = {"entry_price": entry_price}
        if atr is not None:
            features["atr"] = float(atr)

        candidates: list[tuple[float, str, float, int]] = []

        if structure_break_level is not None and structure_break_level > 0:
            candidates.append(
                (
                    float(structure_break_level),
                    "structure_break",
                    0.90,
                    TYPE_RANK["structure_break"],
                )
            )
            notes.append(f"structure_break={structure_break_level:.6g}")

        if side == "LONG":
            for p in swing_lows:
                if not (0 < p < entry_price):
                    continue
                dist_pct = (entry_price - p) / entry_price
                typ, conf, rank = classify_swing(dist_pct, self._micro_swing_pct)
                candidates.append((float(p), typ, conf, rank))
            if (
                volume_profile
                and volume_profile.val > 0
                and volume_profile.val < entry_price
            ):
                candidates.append(
                    (volume_profile.val, "value_area", 0.70, TYPE_RANK["value_area"])
                )
                features["vp_val"] = volume_profile.val
            for ev in liquidity_events:
                if getattr(ev, "side", None) == "bid" and 0 < ev.price < entry_price:
                    candidates.append(
                        (ev.price, "liquidity", 0.55, TYPE_RANK["liquidity"])
                    )
        else:
            for p in swing_highs:
                if not (p > entry_price):
                    continue
                dist_pct = (p - entry_price) / entry_price
                typ, conf, rank = classify_swing(dist_pct, self._micro_swing_pct)
                candidates.append((float(p), typ, conf, rank))
            if (
                volume_profile
                and volume_profile.vah > 0
                and volume_profile.vah > entry_price
            ):
                candidates.append(
                    (volume_profile.vah, "value_area", 0.70, TYPE_RANK["value_area"])
                )
                features["vp_vah"] = volume_profile.vah
            for ev in liquidity_events:
                if getattr(ev, "side", None) == "ask" and ev.price > entry_price:
                    candidates.append(
                        (ev.price, "liquidity", 0.55, TYPE_RANK["liquidity"])
                    )

        valid = [
            c
            for c in candidates
            if abs(c[0] - entry_price) / entry_price <= self._max_stop_pct
        ]
        for c in candidates:
            if c not in valid:
                notes.append(f"skipped {c[1]}@{c[0]:.6g} — exceeds max_stop_pct")

        chosen = select_by_hierarchy(
            valid,
            side=side,
            entry_price=entry_price,
            hierarchy_slack_pct=self._hierarchy_slack_pct,
        )

        if chosen is not None:
            chosen_price, chosen_type, chosen_conf = chosen
            dist_pct = abs(chosen_price - entry_price) / entry_price
            notes.append(
                f"selected {chosen_type}@{chosen_price:.6g} (dist={dist_pct:.2%})"
            )
        else:
            fallback_dist = entry_price * self._fallback_pct
            if atr is not None and atr > 0:
                fallback_dist = max(fallback_dist, atr * 1.5)
            chosen_price = (
                entry_price - fallback_dist if side == "LONG" else entry_price + fallback_dist
            )
            chosen_type = "emergency_fallback"
            chosen_conf = 0.30
            notes.append(f"fallback stop distance={fallback_dist:.6g}")

        buffer = self._compute_buffer(entry_price, atr)
        stop_price = (
            chosen_price - buffer if side == "LONG" else chosen_price + buffer
        )

        if side == "LONG":
            stop_price = min(stop_price, entry_price * (1.0 - self._min_buffer_pct))
            stop_price = max(stop_price, entry_price * (1.0 - self._max_stop_pct))
        else:
            stop_price = max(stop_price, entry_price * (1.0 + self._min_buffer_pct))
            stop_price = min(stop_price, entry_price * (1.0 + self._max_stop_pct))

        distance = abs(stop_price - entry_price)
        return StructuralStop(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            stop_price=round(stop_price, 8),
            distance=round(distance, 8),
            distance_pct=round(distance / entry_price, 6),
            invalidation_type=chosen_type,
            confidence=round(chosen_conf, 4),
            buffer_applied=round(buffer, 8),
            as_of=ts,
            notes=tuple(notes),
            features=features,
        )

    def _compute_buffer(self, entry_price: float, atr: float | None) -> float:
        min_buf = entry_price * self._min_buffer_pct
        if atr is not None and atr > 0:
            return max(min_buf, atr * self._vol_buffer_atr_mult)
        return min_buf
