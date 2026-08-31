"""OpportunityScanner — multi-source market intelligence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.core.exceptions import ModuleNotInitializedError
from aitos.eventbus.redis_bus import EventBus
from aitos.exchange.base import ExchangeAdapter
from aitos.intelligence import indicators
from aitos.intelligence.amt.engine import AMTContext, AMTEngine
from aitos.intelligence.auction import auction_context_score
from aitos.intelligence.footprint import FootprintEngine
from aitos.intelligence.footprint_signals import FootprintSignalEngine
from aitos.intelligence.funding import funding_rate_score
from aitos.intelligence.liquidity import (
    absorption_proxy_score,
    depth_imbalance_score,
    liquidity_intelligence_score,
    liquidity_quality_score,
    liquidity_wall_score,
    sweep_potential_score,
)
from aitos.intelligence.liquidity_tracker import LiquidityTracker
from aitos.intelligence.live_auction import live_auction_score
from aitos.intelligence.live_scanner import LiveScannerCache
from aitos.intelligence.open_interest import oi_trend_score
from aitos.intelligence.order_flow_engine import OrderFlowEngine
from aitos.intelligence.orderflow_liquidity_interaction import (
    FlowLiquidityInteractionEngine,
)
from aitos.intelligence.rl_policy import NeutralRLScorer, RLPolicyScorer
from aitos.logging_setup import get_logger
from aitos.models.trade import Opportunity, TradeSide

logger = get_logger("aitos.intelligence.scanner")
TOPIC_SCAN_COMPLETE = "market.opportunity_scanned"
DEFAULT_WEIGHTS: dict[str, float] = {
    "trend_strength": 0.10,
    "liquidity_quality": 0.10,
    "order_flow_bias": 0.15,
    "auction_context": 0.10,
    "volatility": 0.05,
    "market_regime": 0.10,
    "lead_lag": 0.10,
    "funding_rate": 0.08,
    "open_interest_trend": 0.08,
    "rl_confidence": 0.04,
    "footprint_interaction": 0.10,
}
REGIME_FIT_SCORE = {
    "trending": 9.0,
    "ranging": 4.0,
    "volatile": 3.0,
    "compression": 5.5,
    "expansion": 6.5,
    "unknown": 5.0,
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ScanCandidate:
    symbol: str
    direction: TradeSide
    composite_score: float
    component_scores: dict[str, float]
    component_availability: dict[str, bool]
    rationale: list[str]
    entry_price: float
    atr: float
    regime: str
    scanned_at: str = field(default_factory=_utc_now_iso)


def _volatility_fitness(
    atr_percentile: float, sweet_spot: float = 60.0, tolerance: float = 6.0
) -> float:
    return round(
        max(0.0, min(10.0, 10.0 - abs(atr_percentile - sweet_spot) / tolerance)), 2
    )


def determine_direction(
    structure_direction: str,
    cvd_score: float,
    *,
    structure_bias: str | None = None,
    structure_event: str | None = None,
) -> TradeSide | None:
    """Map structure + CVD into a trade side.

    Optional ``structure_bias`` / ``structure_event`` refine the decision:
    - CVD-only (structure_direction=="none") is blocked when it fights bias.
    - CHOCH may flip with confirming flow even against prior bias.
    """
    bias = (structure_bias or "").lower() or None
    event = (structure_event or "").lower() or None

    # CHOCH: allow structural flip when flow confirms
    if event == "choch":
        if structure_direction == "bullish_bos" and cvd_score >= 5.0:
            return TradeSide.LONG
        if structure_direction == "bearish_bos" and cvd_score <= 5.0:
            return TradeSide.SHORT
        return None

    if structure_direction == "bullish_bos" and cvd_score >= 5.0:
        return TradeSide.LONG
    if structure_direction == "bearish_bos" and cvd_score <= 5.0:
        return TradeSide.SHORT
    if structure_direction == "none":
        if cvd_score >= 6.5:
            if bias == "bearish":
                return None
            return TradeSide.LONG
        if cvd_score <= 3.5:
            if bias == "bullish":
                return None
            return TradeSide.SHORT
    return None
