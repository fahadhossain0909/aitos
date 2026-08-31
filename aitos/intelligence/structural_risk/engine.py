"""Structural Risk Engine — thesis-invalidation stop placement.

Priority order for LONG (mirror for SHORT):

1. Most recent protected swing low / structure break level
2. Volume-profile VAL (acceptance boundary)
3. Significant liquidity pool below entry
4. Fallback: volatility-aware buffer from entry (never pure % risk)

A small liquidity-aware + volatility-aware buffer is added so the stop does
not sit exactly on an obvious stop-cluster.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from aitos.intelligence.amt.volume_profile import VolumeProfile
from aitos.intelligence.liquidity_tracker import LiquidityEvent
from aitos.intelligence.market_state.models import MarketState
from aitos.intelligence.structural_risk.models import StructuralStop
from aitos.logging_setup import get_logger

logger = get_logger("aitos.intelligence.structural_risk")

# Defaults (overridable via config)
DEFAULT_VOL_BUFFER_ATR_MULT = 0.15
DEFAULT_MIN_BUFFER_PCT = 0.0008  # 8 bps
DEFAULT_MAX_STOP_PCT = 0.04  # 4 % hard sanity cap
DEFAULT_FALLBACK_PCT = 0.012  # 1.2 % when no structure available


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class StructuralRiskEngine:
    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        self._vol_buffer_atr_mult = float(
            cfg.get("vol_buffer_atr_mult", DEFAULT_VOL_BUFFER_ATR_MULT)
        )
        self._min_buffer_pct = float(cfg.get("min_buffer_pct", DEFAULT_MIN_BUFFER_PCT))
        self._max_stop_pct = float(cfg.get("max_stop_pct", DEFAULT_MAX_STOP_PCT))
        self._fallback_pct = float(cfg.get("fallback_pct", DEFAULT_FALLBACK_PCT))

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
        """Return the structural invalidation stop for the given side/entry."""
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

        # Collect candidate invalidation levels (on the protective side)
        candidates: list[tuple[float, str, float]] = []  # (price, type, confidence)

        if structure_break_level is not None and structure_break_level > 0:
            candidates.append((float(structure_break_level), "structure_break", 0.85))
            notes.append(f"structure_break={structure_break_level:.6g}")

        if side == "LONG":
            for p in swing_lows:
                if 0 < p < entry_price:
                    candidates.append((float(p), "swing", 0.75))
            if volume_profile and volume_profile.val > 0 and volume_profile.val < entry_price:
                candidates.append((volume_profile.val, "value_area", 0.70))
                features["vp_val"] = volume_profile.val
            for ev in liquidity_events:
                if ev.side == "bid" and 0 < ev.price < entry_price:
                    candidates.append((ev.price, "liquidity", 0.55))
        else:  # SHORT
            for p in swing_highs:
                if p > entry_price:
                    candidates.append((float(p), "swing", 0.75))
            if volume_profile and volume_profile.vah > 0 and volume_profile.vah > entry_price:
                candidates.append((volume_profile.vah, "value_area", 0.70))
                features["vp_vah"] = volume_profile.vah
            for ev in liquidity_events:
                if ev.side == "ask" and ev.price > entry_price:
                    candidates.append((ev.price, "liquidity", 0.55))

        # Choose the tightest *valid* candidate that still respects max stop distance
        chosen_price: float | None = None
        chosen_type = "emergency_fallback"
        chosen_conf = 0.30

        if candidates:
            # For LONG we want the highest level still below entry (closest invalidation)
            # For SHORT we want the lowest level still above entry
            if side == "LONG":
                candidates.sort(key=lambda t: -t[0])  # descending
            else:
                candidates.sort(key=lambda t: t[0])  # ascending

            for price, typ, conf in candidates:
                dist_pct = abs(price - entry_price) / entry_price
                if dist_pct <= self._max_stop_pct:
                    chosen_price = price
                    chosen_type = typ
                    chosen_conf = conf
                    notes.append(f"selected {typ}@{price:.6g} (dist={dist_pct:.2%})")
                    break
                else:
                    notes.append(
                        f"skipped {typ}@{price:.6g} — exceeds max_stop_pct"
                    )

        if chosen_price is None:
            # Fallback: volatility-aware or fixed percentage
            fallback_dist = entry_price * self._fallback_pct
            if atr is not None and atr > 0:
                fallback_dist = max(fallback_dist, atr * 1.5)
            if side == "LONG":
                chosen_price = entry_price - fallback_dist
            else:
                chosen_price = entry_price + fallback_dist
            chosen_type = "emergency_fallback"
            chosen_conf = 0.30
            notes.append(f"fallback stop distance={fallback_dist:.6g}")

        # Apply liquidity / volatility buffer so we don't sit on the exact level
        buffer = self._compute_buffer(entry_price, atr)
        if side == "LONG":
            stop_price = chosen_price - buffer
        else:
            stop_price = chosen_price + buffer

        # Final sanity: never let stop cross entry or exceed max distance
        if side == "LONG":
            stop_price = min(stop_price, entry_price * (1.0 - self._min_buffer_pct))
            floor = entry_price * (1.0 - self._max_stop_pct)
            stop_price = max(stop_price, floor)
        else:
            stop_price = max(stop_price, entry_price * (1.0 + self._min_buffer_pct))
            ceiling = entry_price * (1.0 + self._max_stop_pct)
            stop_price = min(stop_price, ceiling)

        distance = abs(stop_price - entry_price)
        distance_pct = distance / entry_price

        stop = StructuralStop(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            stop_price=round(stop_price, 8),
            distance=round(distance, 8),
            distance_pct=round(distance_pct, 6),
            invalidation_type=chosen_type,
            confidence=round(chosen_conf, 4),
            buffer_applied=round(buffer, 8),
            as_of=ts,
            notes=tuple(notes),
            features=features,
        )
        logger.debug(
            "StructuralStop computed",
            extra={
                "aitos_extra": {
                    "symbol": symbol,
                    "side": side,
                    "stop": stop.stop_price,
                    "type": chosen_type,
                }
            },
        )
        return stop

    def _compute_buffer(self, entry_price: float, atr: float | None) -> float:
        min_buf = entry_price * self._min_buffer_pct
        if atr is not None and atr > 0:
            return max(min_buf, atr * self._vol_buffer_atr_mult)
        return min_buf
