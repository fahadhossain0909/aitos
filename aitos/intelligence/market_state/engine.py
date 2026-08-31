"""Market State Engine — Phase A.

Aggregates signals already produced by the existing intelligence layer
(order-flow engine, liquidity tracker, indicators, live_state, auction)
into one immutable MarketState snapshot.

Design constraints
------------------
* Deterministic given the same inputs.
* Never mutates upstream engines or stores.
* Every classification decision is recorded in the feature bag / notes
  so XAI and the future Exit Intelligence Engine can audit it.
* No ML / RL in this version.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from aitos.intelligence.market_state.models import (
    AuctionState,
    LiquidityBias,
    MarketState,
    MomentumState,
    OrderFlowBias,
    Regime,
    StructureBias,
    VolatilityRegime,
)
from aitos.intelligence.order_flow_engine import OrderFlowFeatures
from aitos.logging_setup import get_logger

logger = get_logger("aitos.intelligence.market_state")

# ---------------------------------------------------------------------------
# Thresholds (tunable via config later; hard-coded for first deterministic cut)
# ---------------------------------------------------------------------------
TREND_STRENGTH_STRONG = 0.65
TREND_STRENGTH_WEAK = 0.35
OF_IMBALANCE_DOMINANT = 0.55  # |imbalance| above this → dominant side
REVERSAL_RISK_BASE = 0.15


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


class MarketStateEngine:
    """Pure function-style engine: inputs in → MarketState out.

    Callers (scanner, lifecycle, path planner) supply the already-computed
    feature objects; the engine never reaches into Redis or ClickHouse itself.
    """

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self._cfg = dict(config or {})

    def compute(
        self,
        *,
        symbol: str,
        mid_price: float,
        order_flow: OrderFlowFeatures | None = None,
        # Optional richer inputs (all may be None in early integration)
        trend_strength: float | None = None,
        atr_pct: float | None = None,
        realized_vol_pct: float | None = None,
        volume_profile_poc: float | None = None,
        value_area_high: float | None = None,
        value_area_low: float | None = None,
        structure_bias_hint: str | None = None,
        liquidity_upside_score: float | None = None,
        liquidity_downside_score: float | None = None,
        timestamp: datetime | None = None,
        extra_features: Mapping[str, float] | None = None,
    ) -> MarketState:
        """Build a MarketState from the supplied signals.

        Missing optional signals degrade gracefully to UNKNOWN / NEUTRAL
        rather than raising; the feature bag always records what was used.
        """
        ts = timestamp or datetime.now(timezone.utc)
        features: dict[str, float] = {}
        notes: list[str] = []

        if extra_features:
            features.update({k: float(v) for k, v in extra_features.items()})

        # ---- Order-flow bias -------------------------------------------------
        of_bias, of_feats, of_notes = self._classify_order_flow(order_flow)
        features.update(of_feats)
        notes.extend(of_notes)

        # ---- Trend / regime --------------------------------------------------
        ts_val = _clamp01(trend_strength if trend_strength is not None else 0.5)
        features["trend_strength"] = ts_val
        regime = self._classify_regime(ts_val, of_bias)
        notes.append(f"regime={regime.value} (trend_strength={ts_val:.2f})")

        # ---- Volatility regime -----------------------------------------------
        vol_regime, vol_feats = self._classify_volatility(atr_pct, realized_vol_pct)
        features.update(vol_feats)

        # ---- Auction state (Volume Profile aware when available) -------------
        auction, auc_feats, auc_notes = self._classify_auction(
            mid_price, volume_profile_poc, value_area_high, value_area_low
        )
        features.update(auc_feats)
        notes.extend(auc_notes)

        # ---- Liquidity bias --------------------------------------------------
        liq_bias, liq_feats = self._classify_liquidity(
            liquidity_upside_score, liquidity_downside_score
        )
        features.update(liq_feats)

        # ---- Momentum --------------------------------------------------------
        momentum, mom_feats = self._classify_momentum(order_flow, ts_val)
        features.update(mom_feats)

        # ---- Structure -------------------------------------------------------
        structure = self._classify_structure(structure_bias_hint, regime)

        # ---- Reversal risk (simple composite for Phase A) --------------------
        reversal_risk = self._estimate_reversal_risk(
            of_bias, momentum, vol_regime, structure, ts_val
        )
        features["reversal_risk"] = reversal_risk

        state = MarketState(
            symbol=symbol,
            timestamp=ts,
            mid_price=float(mid_price),
            regime=regime,
            trend_strength=ts_val,
            volatility_regime=vol_regime,
            auction_state=auction,
            order_flow_bias=of_bias,
            liquidity_bias=liq_bias,
            momentum=momentum,
            structure=structure,
            reversal_risk=reversal_risk,
            features=features,
            notes=tuple(notes),
        )
        logger.debug(
            "MarketState computed",
            extra={
                "aitos_extra": {
                    "symbol": symbol,
                    "regime": regime.value,
                    "of_bias": of_bias.value,
                    "reversal_risk": reversal_risk,
                }
            },
        )
        return state

    # ------------------------------------------------------------------
    # Private classifiers (deterministic, pure)
    # ------------------------------------------------------------------

    def _classify_order_flow(
        self, of: OrderFlowFeatures | None
    ) -> tuple[OrderFlowBias, dict[str, float], list[str]]:
        feats: dict[str, float] = {}
        notes: list[str] = []
        if of is None:
            notes.append("order_flow=missing → NEUTRAL")
            return OrderFlowBias.NEUTRAL, feats, notes

        feats["of_imbalance"] = float(of.imbalance)
        feats["of_delta"] = float(of.delta)
        feats["of_cvd"] = float(of.cvd)
        feats["of_aggression"] = float(of.aggression)
        feats["of_buy_ratio"] = float(of.buy_ratio)

        imb = of.imbalance  # typically already normalised ~[-1, 1] or [0, 10]
        # Normalise common 0-10 bias_score scale if present
        if hasattr(of, "bias_score") and of.bias_score is not None:
            # bias_score 0-10 → map to [-1, 1] roughly
            normalised = (float(of.bias_score) - 5.0) / 5.0
        else:
            normalised = float(imb)
            if abs(normalised) > 1.5:  # probably 0-10 scale
                normalised = (normalised - 5.0) / 5.0

        feats["of_normalised"] = normalised

        if normalised >= OF_IMBALANCE_DOMINANT:
            bias = OrderFlowBias.BUYER_DOMINANT
        elif normalised <= -OF_IMBALANCE_DOMINANT:
            bias = OrderFlowBias.SELLER_DOMINANT
        else:
            bias = OrderFlowBias.NEUTRAL

        notes.append(f"order_flow={bias.value} (norm={normalised:.2f})")
        return bias, feats, notes

    def _classify_regime(
        self, trend_strength: float, of_bias: OrderFlowBias
    ) -> Regime:
        if trend_strength >= TREND_STRENGTH_STRONG:
            if of_bias == OrderFlowBias.SELLER_DOMINANT:
                return Regime.TRANSITION  # strong trend but opposing flow
            return Regime.TRENDING_UP if of_bias != OrderFlowBias.SELLER_DOMINANT else Regime.TRENDING_DOWN
        if trend_strength <= TREND_STRENGTH_WEAK:
            return Regime.RANGE
        # medium strength — let OF decide direction if clear
        if of_bias == OrderFlowBias.BUYER_DOMINANT:
            return Regime.TRENDING_UP
        if of_bias == OrderFlowBias.SELLER_DOMINANT:
            return Regime.TRENDING_DOWN
        return Regime.TRANSITION

    def _classify_volatility(
        self,
        atr_pct: float | None,
        realized_vol_pct: float | None,
    ) -> tuple[VolatilityRegime, dict[str, float]]:
        feats: dict[str, float] = {}
        # Prefer realised vol when present; fall back to ATR%
        vol = realized_vol_pct if realized_vol_pct is not None else atr_pct
        if vol is None:
            return VolatilityRegime.NORMAL, feats

        feats["vol_pct"] = float(vol)
        # Heuristic thresholds (crypto futures typically higher than equities)
        if vol < 0.8:
            return VolatilityRegime.CONTRACTING, feats
        if vol > 2.5:
            return VolatilityRegime.EXPANDING, feats
        return VolatilityRegime.NORMAL, feats

    def _classify_auction(
        self,
        mid: float,
        poc: float | None,
        vah: float | None,
        val: float | None,
    ) -> tuple[AuctionState, dict[str, float], list[str]]:
        feats: dict[str, float] = {}
        notes: list[str] = []
        if poc is not None:
            feats["vp_poc"] = float(poc)
        if vah is not None:
            feats["vp_vah"] = float(vah)
        if val is not None:
            feats["vp_val"] = float(val)

        if vah is not None and val is not None and poc is not None:
            if mid > vah:
                state = AuctionState.ACCEPTANCE_ABOVE_VALUE
            elif mid < val:
                state = AuctionState.ACCEPTANCE_BELOW_VALUE
            else:
                state = AuctionState.ACCEPTANCE_INSIDE_VALUE
            notes.append(f"auction={state.value} (mid vs VA)")
            return state, feats, notes

        notes.append("auction=UNKNOWN (no volume-profile levels)")
        return AuctionState.UNKNOWN, feats, notes

    def _classify_liquidity(
        self,
        upside: float | None,
        downside: float | None,
    ) -> tuple[LiquidityBias, dict[str, float]]:
        feats: dict[str, float] = {}
        if upside is not None:
            feats["liq_upside"] = float(upside)
        if downside is not None:
            feats["liq_downside"] = float(downside)

        if upside is None and downside is None:
            return LiquidityBias.UNKNOWN, feats

        u = float(upside or 0.0)
        d = float(downside or 0.0)
        if u + d < 1e-9:
            return LiquidityBias.THIN, feats
        if u > d * 1.4:
            return LiquidityBias.UPSIDE_LIQUIDITY_HIGH, feats
        if d > u * 1.4:
            return LiquidityBias.DOWNSIDE_LIQUIDITY_HIGH, feats
        return LiquidityBias.BALANCED, feats

    def _classify_momentum(
        self,
        of: OrderFlowFeatures | None,
        trend_strength: float,
    ) -> tuple[MomentumState, dict[str, float]]:
        feats: dict[str, float] = {}
        if of is None:
            # Fall back to trend strength alone
            if trend_strength >= 0.7:
                return MomentumState.STRONG, feats
            if trend_strength >= 0.4:
                return MomentumState.MODERATING, feats
            return MomentumState.WEAK, feats

        aggression = abs(float(of.aggression))
        feats["momentum_aggression"] = aggression
        if aggression >= 0.7 and trend_strength >= 0.6:
            return MomentumState.STRONG, feats
        if aggression < 0.25:
            return MomentumState.EXHAUSTED, feats
        if aggression < 0.45 or trend_strength < 0.4:
            return MomentumState.WEAK, feats
        return MomentumState.MODERATING, feats

    def _classify_structure(
        self, hint: str | None, regime: Regime
    ) -> StructureBias:
        if hint:
            h = hint.upper()
            if h in ("BULLISH", "BEARISH", "RANGE", "BROKEN"):
                return StructureBias(h)
        # Derive from regime as fallback
        if regime == Regime.TRENDING_UP:
            return StructureBias.BULLISH
        if regime == Regime.TRENDING_DOWN:
            return StructureBias.BEARISH
        if regime == Regime.RANGE:
            return StructureBias.RANGE
        return StructureBias.RANGE

    def _estimate_reversal_risk(
        self,
        of_bias: OrderFlowBias,
        momentum: MomentumState,
        vol: VolatilityRegime,
        structure: StructureBias,
        trend_strength: float,
    ) -> float:
        risk = REVERSAL_RISK_BASE
        if momentum in (MomentumState.WEAK, MomentumState.EXHAUSTED):
            risk += 0.18
        if vol == VolatilityRegime.EXPANDING:
            risk += 0.12
        if structure == StructureBias.BROKEN:
            risk += 0.30
        # OF opposing the prevailing structure increases risk
        if structure == StructureBias.BULLISH and of_bias == OrderFlowBias.SELLER_DOMINANT:
            risk += 0.20
        if structure == StructureBias.BEARISH and of_bias == OrderFlowBias.BUYER_DOMINANT:
            risk += 0.20
        if trend_strength < 0.3:
            risk += 0.10
        return _clamp01(risk)
