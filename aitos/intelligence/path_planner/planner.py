"""Market Path Planner — deterministic destination ranking.

Sources (all optional; missing sources simply contribute fewer candidates):

* VolumeProfile  → POC, VAH, VAL, HVN, LVN
* Liquidity events / walls → resting liquidity pools
* Prior swing highs / lows
* MarketState regime & order-flow bias (tilts probability mass)

Probability is a simple, fully explainable score — not a calibrated
statistical forecast. Later phases can replace the scoring function with
an ML model while keeping the same PathPlan contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from aitos.intelligence.amt.volume_profile import VolumeProfile
from aitos.intelligence.liquidity_tracker import LiquidityEvent
from aitos.intelligence.market_state.models import MarketState, OrderFlowBias, Regime
from aitos.intelligence.path_planner.models import PathDestination, PathPlan
from aitos.logging_setup import get_logger

logger = get_logger("aitos.intelligence.path_planner")

# Minimum relative distance to consider a level (avoid noise at current price)
MIN_REL_DISTANCE = 0.0004  # 4 bps
MAX_DESTINATIONS_PER_SIDE = 6


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


class MarketPathPlanner:
    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self._cfg = dict(config or {})
        self._max_per_side = int(
            self._cfg.get("max_destinations_per_side", MAX_DESTINATIONS_PER_SIDE)
        )

    def plan(
        self,
        *,
        market_state: MarketState,
        volume_profile: VolumeProfile | None = None,
        liquidity_events: Sequence[LiquidityEvent] = (),
        prior_highs: Sequence[float] = (),
        prior_lows: Sequence[float] = (),
        swing_highs: Sequence[float] = (),
        swing_lows: Sequence[float] = (),
        extra_levels: Sequence[tuple[float, str]] = (),
    ) -> PathPlan:
        """Build a PathPlan from the supplied structural / liquidity inputs."""
        price = market_state.mid_price
        symbol = market_state.symbol
        ts = market_state.timestamp or datetime.now(timezone.utc)
        candidates: list[dict[str, Any]] = []
        notes: list[str] = []
        features: dict[str, float] = {"current_price": price}

        # ---- Volume Profile levels ------------------------------------------
        if volume_profile is not None and volume_profile.total_volume > 0:
            candidates.extend(self._from_volume_profile(volume_profile, price))
            features["vp_poc"] = volume_profile.poc
            features["vp_vah"] = volume_profile.vah
            features["vp_val"] = volume_profile.val
            notes.append(
                f"vp: poc={volume_profile.poc:.4g} vah={volume_profile.vah:.4g} "
                f"val={volume_profile.val:.4g}"
            )

        # ---- Liquidity events / walls ---------------------------------------
        candidates.extend(self._from_liquidity_events(liquidity_events, price))

        # ---- Prior extremes & swings ----------------------------------------
        for p in prior_highs:
            candidates.append(
                self._candidate(p, price, "prior_high", "none", base_prob=0.55)
            )
        for p in prior_lows:
            candidates.append(
                self._candidate(p, price, "prior_low", "none", base_prob=0.55)
            )
        for p in swing_highs:
            candidates.append(
                self._candidate(p, price, "swing", "none", base_prob=0.50)
            )
        for p in swing_lows:
            candidates.append(
                self._candidate(p, price, "swing", "none", base_prob=0.50)
            )

        # ---- Explicit extra levels (caller-supplied) ------------------------
        for p, kind in extra_levels:
            candidates.append(self._candidate(p, price, kind, "none", base_prob=0.45))

        # ---- Filter noise & de-duplicate ------------------------------------
        candidates = self._dedupe_and_filter(candidates, price)

        # ---- Regime / OF tilt -----------------------------------------------
        candidates = self._apply_regime_tilt(candidates, market_state)

        # ---- Split upside / downside & rank ---------------------------------
        upside = [
            self._to_destination(c)
            for c in sorted(
                (c for c in candidates if c["price"] > price),
                key=lambda c: (-c["probability"], c["distance"]),
            )[: self._max_per_side]
        ]
        downside = [
            self._to_destination(c)
            for c in sorted(
                (c for c in candidates if c["price"] < price),
                key=lambda c: (-c["probability"], c["distance"]),
            )[: self._max_per_side]
        ]

        plan = PathPlan(
            symbol=symbol,
            current_price=price,
            upside=tuple(upside),
            downside=tuple(downside),
            as_of=ts,
            features=features,
            notes=tuple(notes),
        )
        logger.debug(
            "PathPlan built",
            extra={
                "aitos_extra": {
                    "symbol": symbol,
                    "upside_n": len(upside),
                    "downside_n": len(downside),
                }
            },
        )
        return plan

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _from_volume_profile(
        self, vp: VolumeProfile, price: float
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        # POC — high acceptance, moderate continuation probability
        if vp.poc > 0:
            out.append(
                self._candidate(
                    vp.poc, price, "POC", "none", base_prob=0.60, confidence=0.75
                )
            )
        # VAH / VAL — value area boundaries
        if vp.vah > 0:
            out.append(
                self._candidate(
                    vp.vah, price, "vah", "none", base_prob=0.58, confidence=0.70
                )
            )
        if vp.val > 0:
            out.append(
                self._candidate(
                    vp.val, price, "val", "none", base_prob=0.58, confidence=0.70
                )
            )
        # LVN — fast-travel zones (higher continuation once entered)
        for p in vp.lvn:
            out.append(
                self._candidate(
                    p, price, "LVN", "none", base_prob=0.62, confidence=0.65
                )
            )
        # HVN — stall / acceptance zones (lower continuation, more reaction)
        for p in vp.hvn:
            out.append(
                self._candidate(
                    p, price, "HVN", "none", base_prob=0.48, confidence=0.60
                )
            )
        return out

    def _from_liquidity_events(
        self, events: Sequence[LiquidityEvent], price: float
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for ev in events:
            if ev.price <= 0:
                continue
            # Stronger score → higher probability of being a magnet / target
            base = 0.40 + min(0.35, ev.score / 10.0 * 0.35)
            liq_type = "resting"
            if ev.kind in ("sweep",):
                liq_type = "stop_cluster"
            out.append(
                self._candidate(
                    ev.price,
                    price,
                    "liquidity_pool",
                    liq_type,
                    base_prob=base,
                    confidence=0.55,
                    notes=f"{ev.kind}/{ev.side} score={ev.score}",
                )
            )
        return out

    def _candidate(
        self,
        level: float,
        current: float,
        structure_type: str,
        liquidity_type: str,
        base_prob: float = 0.5,
        confidence: float = 0.5,
        notes: str = "",
    ) -> dict[str, Any]:
        dist = abs(level - current)
        rel = dist / current if current > 0 else 0.0
        # Closer levels get a mild probability boost; very far levels decay
        proximity_factor = 1.0
        if rel < 0.005:
            proximity_factor = 1.15
        elif rel > 0.03:
            proximity_factor = 0.75
        elif rel > 0.015:
            proximity_factor = 0.90

        horizon = "scalp" if rel < 0.008 else ("intraday" if rel < 0.025 else "swing")
        return {
            "price": float(level),
            "probability": _clamp01(base_prob * proximity_factor),
            "distance": dist,
            "market_structure_type": structure_type,
            "liquidity_type": liquidity_type,
            "expected_horizon": horizon,
            "confidence": _clamp01(confidence),
            "notes": notes,
        }

    def _dedupe_and_filter(
        self, candidates: list[dict[str, Any]], price: float
    ) -> list[dict[str, Any]]:
        """Drop levels too close to current price and merge near-duplicates."""
        if price <= 0:
            return []
        filtered = [
            c for c in candidates if abs(c["price"] - price) / price >= MIN_REL_DISTANCE
        ]
        # Sort by price and merge levels within 0.05% of each other
        filtered.sort(key=lambda c: c["price"])
        merged: list[dict[str, Any]] = []
        for c in filtered:
            if not merged:
                merged.append(c)
                continue
            prev = merged[-1]
            if abs(c["price"] - prev["price"]) / price < 0.0005:
                # Keep the higher-probability one, combine notes
                if c["probability"] > prev["probability"]:
                    c["notes"] = (prev["notes"] + " | " + c["notes"]).strip(" |")
                    merged[-1] = c
                else:
                    prev["notes"] = (prev["notes"] + " | " + c["notes"]).strip(" |")
            else:
                merged.append(c)
        return merged

    def _apply_regime_tilt(
        self, candidates: list[dict[str, Any]], state: MarketState
    ) -> list[dict[str, Any]]:
        """Tilt probability mass in the direction of the prevailing regime / OF."""
        tilt_up = 1.0
        tilt_down = 1.0
        if state.regime == Regime.TRENDING_UP:
            tilt_up = 1.20
            tilt_down = 0.80
        elif state.regime == Regime.TRENDING_DOWN:
            tilt_up = 0.80
            tilt_down = 1.20
        if state.order_flow_bias == OrderFlowBias.BUYER_DOMINANT:
            tilt_up *= 1.10
            tilt_down *= 0.90
        elif state.order_flow_bias == OrderFlowBias.SELLER_DOMINANT:
            tilt_up *= 0.90
            tilt_down *= 1.10

        price = state.mid_price
        for c in candidates:
            if c["price"] > price:
                c["probability"] = _clamp01(c["probability"] * tilt_up)
            else:
                c["probability"] = _clamp01(c["probability"] * tilt_down)
        return candidates

    def _to_destination(self, c: dict[str, Any]) -> PathDestination:
        return PathDestination(
            price=c["price"],
            probability=round(c["probability"], 4),
            distance=round(c["distance"], 6),
            market_structure_type=c["market_structure_type"],
            liquidity_type=c["liquidity_type"],
            expected_horizon=c["expected_horizon"],
            confidence=round(c["confidence"], 4),
            notes=c.get("notes", ""),
        )
