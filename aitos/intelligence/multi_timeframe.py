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
    bias = "neutral"
    if adx >= 20.0 and gap >= 3.0:
        bias = "bullish" if plus_di > minus_di else "bearish"
    reason = "fresh_completed_candles"
    if stale:
        reason = "stale_candles"
    elif bias == "neutral":
        reason = "weak_or_choppy_trend"
    context = TimeframeContext(
        timeframe=timeframe,
        bias=bias if not stale else "unknown",
        strength=min(10.0, max(0.0, adx / 10.0)),
        regime=str(indicators.classify_regime(candles)),
        available=not stale,
        completed=True,
        diagnostics={
            "adx": adx,
            "plus_di": float(plus_di),
            "minus_di": float(minus_di),
            "di_gap": gap,
            "structure_bias": str(structure.bias),
            "latest_closed_at": latest.isoformat(),
            "age_seconds": age,
            "max_age_seconds": max_age,
            "stale": stale,
            "reason": reason,
        },
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
        # LTF evidence uses entry bars plus flow. Structure and momentum remain
        # on a common LONG-oriented 0..10 scale; SHORT is mirrored centrally.
        structure_score = (
            8.0
            if structure_bias == "bullish"
            else 2.0 if structure_bias == "bearish" else 5.0
        )
        momentum_score = float(indicators.momentum_score(entry_candles))
        evidence = DirectionalEvidence(
            structure=structure_score,
            order_flow=float(flow_score),
            momentum=momentum_score,
            liquidity=5.0,
            location=5.0,
            participation=5.0,
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
        }
        return MultiTimeframeResult(
            decision.accepted, decision, snapshots, evidence, diagnostics
        )
