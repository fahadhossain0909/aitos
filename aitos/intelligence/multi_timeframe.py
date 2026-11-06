"""Six-timeframe, candle-close-aware market context orchestration.

The analyzer is intentionally side-effect free except for the supplied cache/fetch
boundary. It never places orders. Every frame carries explicit freshness and
completion diagnostics; stale/missing frames are not silently neutralized.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from aitos.intelligence import indicators
from aitos.intelligence.adaptive_mtf import (
    DirectionalEvidence,
    HTFDecision,
    TimeframeContext,
    evaluate_adaptive_htf,
)
from aitos.intelligence.htf_cache import HigherTimeframeCandleCache, timeframe_delta
from aitos.models.trade import TradeSide

REQUIRED_TIMEFRAMES = ("1d", "4h", "1h", "15m", "5m", "1m")
DEFAULT_LOOKBACK = 80

TIMEFRAME_ROLES = {
    "1d": ("macro_regime", "major_swings", "premium_discount"),
    "4h": ("macro_structure", "supply_demand", "major_liquidity"),
    "1h": ("trend_structure", "bos_choch", "vwap_location"),
    "15m": ("setup", "momentum", "cvd_divergence"),
    "5m": ("trigger", "sweep_reclaim", "relative_participation"),
    "1m": ("micro_price_context", "micro_momentum", "wick_rejection"),
}
TIMEFRAME_WEIGHTS = {
    "1d": 2.0,
    "4h": 1.8,
    "1h": 1.0,
    "15m": 0.8,
    "5m": 0.6,
    "1m": 0.4,
}
TIMEFRAME_EVIDENCE_FIELDS = {
    "1d": ("structure", "location", "liquidity", "displacement_atr"),
    "4h": ("structure", "liquidity", "participation", "displacement_atr"),
    "1h": ("structure", "momentum", "location", "displacement_atr"),
    "15m": ("momentum", "order_flow", "participation", "displacement_atr"),
    "5m": (
        "liquidity",
        "participation",
        "lower_sweep_reclaim",
        "upper_sweep_reclaim",
        "displacement_atr",
    ),
    "1m": ("momentum", "liquidity", "location", "displacement_atr"),
}


def _clamp_score(value: float) -> float:
    return round(min(10.0, max(0.0, float(value))), 4)


def _candle_evidence(candles: list[Any]) -> dict[str, float]:
    """Build reproducible OHLCV evidence; no fabricated neutral placeholders."""
    if len(candles) < 21:
        raise ValueError("at least 21 completed candles are required")
    structure = indicators.analyze_market_structure(candles)
    momentum = _clamp_score(indicators.momentum_score(candles))
    cvd = _clamp_score(indicators.cvd_trend_score(candles))
    recent = candles[-20:]
    prior = candles[-40:-20]
    avg_recent = sum(float(k.volume) for k in recent) / len(recent)
    avg_prior = (
        sum(float(k.volume) for k in prior) / len(prior) if prior else avg_recent
    )
    relative_volume = avg_recent / avg_prior if avg_prior > 0 else 1.0
    highest = max(float(k.high) for k in candles[-21:-1])
    lowest = min(float(k.low) for k in candles[-21:-1])
    last = candles[-1]
    close, high, low = float(last.close), float(last.high), float(last.low)
    open_price = float(last.open)
    span = max(high - low, 1e-12)
    volume_participation = min(1.0, max(0.0, (relative_volume - 1.0) / 1.5))
    signed_body = (close - open_price) / span
    participation = _clamp_score(5.0 + 5.0 * volume_participation * signed_body)
    range_high = max(float(k.high) for k in candles[-20:])
    range_low = min(float(k.low) for k in candles[-20:])
    range_span = max(range_high - range_low, 1e-12)
    # Location is premium/discount within the rolling range, not the close's
    # position inside the current candle.
    close_location = _clamp_score(10.0 * (close - range_low) / range_span)
    lower_sweep = low < lowest and close > lowest
    upper_sweep = high > highest and close < highest
    lower_wick = max(0.0, min(close, open_price) - low) / span
    upper_wick = max(0.0, high - max(close, open_price)) / span
    true_ranges = []
    for index in range(max(1, len(candles) - 14), len(candles)):
        candle = candles[index]
        previous_close = float(candles[index - 1].close)
        true_ranges.append(
            max(
                float(candle.high) - float(candle.low),
                abs(float(candle.high) - previous_close),
                abs(float(candle.low) - previous_close),
            )
        )
    atr = sum(true_ranges) / len(true_ranges) if true_ranges else span
    displacement_atr = abs(close - open_price) / max(atr, 1e-12)
    bullish_sweep_displacement = (
        lower_sweep and close > open_price and displacement_atr >= 1.0
    )
    bearish_sweep_displacement = (
        upper_sweep and close < open_price and displacement_atr >= 1.0
    )
    sweep_pressure = (
        0.0
        if lower_sweep and upper_sweep
        else (
            4.0
            if bullish_sweep_displacement
            else (
                -4.0
                if bearish_sweep_displacement
                else 3.0 if lower_sweep else -3.0 if upper_sweep else 0.0
            )
        )
    )
    liquidity = _clamp_score(5.0 + sweep_pressure + 2.0 * (lower_wick - upper_wick))
    return {
        "structure": _clamp_score(structure.structure_score),
        "order_flow": cvd,
        "momentum": momentum,
        "liquidity": liquidity,
        "location": close_location,
        "participation": participation,
        "relative_volume": round(relative_volume, 4),
        "lower_sweep_reclaim": float(lower_sweep),
        "upper_sweep_reclaim": float(upper_sweep),
        "displacement_atr": round(displacement_atr, 4),
        "bullish_sweep_displacement": float(bullish_sweep_displacement),
        "bearish_sweep_displacement": float(bearish_sweep_displacement),
    }


@dataclass(frozen=True, slots=True)
class TimeframeSnapshot:
    timeframe: str
    candles: tuple[Any, ...]
    context: TimeframeContext
    cache_diagnostics: dict[str, Any]
    freshness_seconds: float | None


@dataclass(frozen=True, slots=True)
class MultiTimeframeResult:
    accepted: bool
    decision: HTFDecision
    snapshots: tuple[TimeframeSnapshot, ...]
    evidence: DirectionalEvidence
    diagnostics: dict[str, Any]


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _frame_context(
    timeframe: str,
    candles: list[Any],
    *,
    now: datetime,
    cache_diagnostics: dict[str, Any],
    freshness_grace_seconds: float,
) -> TimeframeSnapshot:
    if len(candles) < 30:
        context = TimeframeContext(
            timeframe,
            "unknown",
            0.0,
            available=False,
            completed=False,
            diagnostics={"reason": "insufficient_history"},
        )
        return TimeframeSnapshot(
            timeframe, tuple(candles), context, cache_diagnostics, None
        )
    latest = _utc(candles[-1].close_time)
    age = max(0.0, (now - latest).total_seconds())
    max_age = timeframe_delta(timeframe).total_seconds() + freshness_grace_seconds
    stale = bool(cache_diagnostics.get("stale")) or age > max_age
    adx = float(indicators.adx(candles))
    plus_di, minus_di = indicators.di_plus_minus(candles)
    gap = abs(float(plus_di) - float(minus_di))
    structure = indicators.analyze_market_structure(candles)
    candle_evidence = _candle_evidence(candles)
    di_bias = "bullish" if plus_di > minus_di else "bearish"
    structure_bias = str(structure.bias).lower()
    bias = "neutral"
    if adx >= 20.0 and gap >= 3.0:
        # Conflicting confirmed structure and directional movement are mixed.
        if structure_bias in {"bullish", "bearish"} and structure_bias != di_bias:
            bias = "neutral"
        else:
            bias = di_bias
    reason = "fresh_completed_candles"
    if stale:
        reason = "stale_candles"
    elif bias == "neutral":
        reason = "weak_or_choppy_trend"
    context = TimeframeContext(
        timeframe=timeframe,
        bias=bias if not stale else "unknown",
        strength=min(10.0, max(0.0, (adx - 10.0) / 3.0)),
        regime=str(indicators.classify_regime(candles)),
        available=not stale,
        completed=True,
        diagnostics={
            "adx": adx,
            "plus_di": float(plus_di),
            "minus_di": float(minus_di),
            "di_gap": gap,
            "structure_bias": structure_bias,
            "directional_index_bias": di_bias,
            "evidence": candle_evidence,
            "role": list(TIMEFRAME_ROLES[timeframe]),
            "role_evidence": {
                field: candle_evidence[field]
                for field in TIMEFRAME_EVIDENCE_FIELDS[timeframe]
            },
            "latest_closed_at": latest.isoformat(),
            "age_seconds": age,
            "max_age_seconds": max_age,
            "stale": stale,
            "reason": reason,
        },
        weight=TIMEFRAME_WEIGHTS[timeframe],
    )
    return TimeframeSnapshot(timeframe, tuple(candles), context, cache_diagnostics, age)


class MultiTimeframeAnalyzer:
    """Fetch and analyze 1D/4H/1H/15M/5M/1M completed candle contexts."""

    def __init__(
        self,
        cache: HigherTimeframeCandleCache,
        *,
        lookback: int = DEFAULT_LOOKBACK,
        freshness_grace_seconds: float = 15.0,
    ) -> None:
        if lookback < 30 or freshness_grace_seconds < 0:
            raise ValueError("lookback must be >=30 and freshness grace non-negative")
        self.cache = cache
        self.lookback = lookback
        self.freshness_grace_seconds = freshness_grace_seconds

    async def analyze(
        self,
        exchange: Any,
        symbol: str,
        side: TradeSide,
        entry_candles: list[Any],
        *,
        flow_score: float,
        structure_bias: str,
        allow_neutral: bool = True,
        neutral_min_score: float = 6.0,
        countertrend_min_score: float = 7.0,
        alignment_balance_threshold: float = 0.25,
    ) -> MultiTimeframeResult:
        now = _utc(datetime.now(timezone.utc))

        async def load(tf: str) -> TimeframeSnapshot:
            candles, cache_diag = await self.cache.get(
                symbol,
                tf,
                self.lookback,
                lambda: exchange.fetch_klines(symbol, tf, limit=self.lookback),
            )
            return _frame_context(
                tf,
                candles,
                now=now,
                cache_diagnostics=cache_diag,
                freshness_grace_seconds=self.freshness_grace_seconds,
            )

        snapshots = tuple(
            await asyncio.gather(*(load(tf) for tf in REQUIRED_TIMEFRAMES))
        )
        # Entry-timeframe evidence is computed from completed OHLCV bars.
        # Trade-derived flow is retained, while candle CVD is exposed separately.
        candle_scores = _candle_evidence(entry_candles)
        evidence = DirectionalEvidence(
            structure=candle_scores["structure"],
            order_flow=_clamp_score(float(flow_score)),
            momentum=candle_scores["momentum"],
            liquidity=candle_scores["liquidity"],
            location=candle_scores["location"],
            participation=candle_scores["participation"],
        )
        # All six frames are part of the requested contract. A partial stack
        # is not silently promoted to a complete multi-timeframe decision.
        missing = [item.timeframe for item in snapshots if not item.context.available]
        if missing:
            decision = HTFDecision(
                False,
                "incomplete_stack",
                0.0,
                "required_timeframe_unavailable_or_stale",
                {"missing_or_stale_timeframes": missing},
            )
        else:
            decision = evaluate_adaptive_htf(
                side,
                [item.context for item in snapshots],
                ltf=evidence,
                min_aligned_score=neutral_min_score,
                countertrend_score=countertrend_min_score,
                alignment_balance_threshold=alignment_balance_threshold,
                allow_neutral=allow_neutral,
                require_any_context=True,
            )
        diagnostics = {
            "required_timeframes": list(REQUIRED_TIMEFRAMES),
            "available_timeframes": [
                s.timeframe for s in snapshots if s.context.available
            ],
            "stale_timeframes": [
                s.timeframe for s in snapshots if s.context.diagnostics.get("stale")
            ],
            "unavailable_timeframes": [
                s.timeframe for s in snapshots if not s.context.available
            ],
            "timeframes": {
                s.timeframe: {
                    "bias": s.context.bias,
                    "strength": s.context.strength,
                    "regime": s.context.regime,
                    "cache": s.cache_diagnostics,
                    **dict(s.context.diagnostics),
                }
                for s in snapshots
            },
            "decision_mode": decision.mode,
            "decision_reason": decision.reason,
            "ltf_score": evidence.score_for(side),
            "ltf_evidence": evidence.as_dict(),
            "candle_evidence": candle_scores,
            "timeframe_roles": {
                tf: list(roles) for tf, roles in TIMEFRAME_ROLES.items()
            },
            "timeframe_weights": dict(TIMEFRAME_WEIGHTS),
        }
        return MultiTimeframeResult(
            decision.accepted, decision, snapshots, evidence, diagnostics
        )
