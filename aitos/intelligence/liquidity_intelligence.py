"""Structure-aware liquidity mapping, sweep classification and risk intelligence.

The engine is deliberately evidence-based: it estimates liquidity pools from
completed candles and explicit reference levels. It never claims to observe
hidden stop orders or infer participant intent. All scores are research
features until calibrated on point-in-time, out-of-sample data.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from math import isfinite
from statistics import mean, pstdev
from typing import Any, Iterable, Mapping, Sequence
import uuid


class LiquiditySide(str, Enum):
    BUY_SIDE = "buy_side"
    SELL_SIDE = "sell_side"


class LiquiditySource(str, Enum):
    EQUAL_HIGHS = "equal_highs"
    EQUAL_LOWS = "equal_lows"
    SWING_HIGH = "swing_high"
    SWING_LOW = "swing_low"
    LOOKBACK_HIGH = "lookback_high"
    LOOKBACK_LOW = "lookback_low"
    EXTERNAL_REFERENCE = "external_reference"


class SweepState(str, Enum):
    SWEPT = "swept"
    REJECTION_PENDING = "rejection_pending"
    REJECTED = "rejected"
    ACCEPTANCE_PENDING = "acceptance_pending"
    ACCEPTED = "accepted"
    INDECISION = "indecision"


class LiquidityAction(str, Enum):
    SHADOW = "shadow"
    WAIT = "wait"
    BLOCK_ENTRY = "block_entry"
    REVERSAL_LONG = "reversal_long"
    REVERSAL_SHORT = "reversal_short"
    CONTINUATION_LONG = "continuation_long"
    CONTINUATION_SHORT = "continuation_short"


@dataclass(frozen=True)
class Candle:
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    timestamp: str = ""
    is_closed: bool = True

    @classmethod
    def from_value(cls, value: Any) -> "Candle":
        def get(name: str, default: Any = None) -> Any:
            if isinstance(value, Mapping):
                return value.get(name, default)
            return getattr(value, name, default)

        candle = cls(
            open=float(get("open")),
            high=float(get("high")),
            low=float(get("low")),
            close=float(get("close")),
            volume=float(get("volume", 0.0) or 0.0),
            timestamp=str(get("close_time", get("timestamp", "")) or ""),
            is_closed=bool(get("is_closed", True)),
        )
        if not all(isfinite(x) for x in (candle.open, candle.high, candle.low, candle.close, candle.volume)):
            raise ValueError("candle values must be finite")
        if candle.low <= 0 or candle.high < candle.low:
            raise ValueError("candle price range is invalid")
        if not candle.low <= candle.open <= candle.high or not candle.low <= candle.close <= candle.high:
            raise ValueError("candle open/close must be inside high/low")
        if candle.volume < 0:
            raise ValueError("candle volume cannot be negative")
        return candle


@dataclass(frozen=True)
class LiquidityLevel:
    level_id: str
    symbol: str
    side: LiquiditySide
    price: float
    source: LiquiditySource
    timeframe: str
    strength: float
    touch_count: int
    first_seen: str = ""
    last_seen: str = ""
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.symbol or not isfinite(self.price) or self.price <= 0:
            raise ValueError("liquidity level requires symbol and positive finite price")
        if not 0.0 <= self.strength <= 100.0:
            raise ValueError("strength must be in [0, 100]")
        if self.touch_count < 1:
            raise ValueError("touch_count must be positive")


@dataclass(frozen=True)
class SweepEvent:
    event_id: str
    level: LiquidityLevel
    direction: str
    sweep_price: float
    penetration: float
    candle_index: int
    candle_timestamp: str
    wick_ratio: float
    volume_zscore: float
    state: SweepState = SweepState.SWEPT
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrderFlowEvidence:
    cvd_score: float = 5.0
    ofi_score: float = 5.0
    fresh: bool = False
    confidence: float = 0.0
    bid_replenishment: float = 0.0
    ask_replenishment: float = 0.0
    aggressive_buy_ratio: float = 0.5

    def __post_init__(self) -> None:
        for name in ("cvd_score", "ofi_score"):
            value = float(getattr(self, name))
            if not isfinite(value) or not 0.0 <= value <= 10.0:
                raise ValueError(name + " must be finite and in [0, 10]")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if not 0.0 <= self.aggressive_buy_ratio <= 1.0:
            raise ValueError("aggressive_buy_ratio must be in [0, 1]")


@dataclass(frozen=True)
class SweepClassification:
    state: SweepState
    action: LiquidityAction
    confidence: float
    evidence: tuple[str, ...]
    bars_observed: int


@dataclass(frozen=True)
class LiquidityRisk:
    score: float
    level_distance_atr: float | None
    nearest_level_id: str | None
    reasons: tuple[str, ...]
    action: LiquidityAction


@dataclass(frozen=True)
class StopPlan:
    side: str
    entry_price: float
    invalidation_price: float
    stop_price: float
    distance: float
    distance_pct: float
    buffer: float
    allowed: bool
    reason: str


@dataclass(frozen=True)
class LiquidityAnalysis:
    symbol: str
    timeframe: str
    levels: tuple[LiquidityLevel, ...]
    sweeps: tuple[SweepEvent, ...]
    latest_classification: SweepClassification | None
    risk: LiquidityRisk
    mode: str = "shadow"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "levels": [asdict(item) for item in self.levels],
            "sweeps": [
                {
                    **asdict(item),
                    "level": asdict(item.level),
                    "state": item.state.value,
                }
                for item in self.sweeps
            ],
            "latest_classification": (
                {
                    "state": self.latest_classification.state.value,
                    "action": self.latest_classification.action.value,
                    "confidence": self.latest_classification.confidence,
                    "evidence": list(self.latest_classification.evidence),
                    "bars_observed": self.latest_classification.bars_observed,
                }
                if self.latest_classification
                else None
            ),
            "risk": {
                "score": self.risk.score,
                "level_distance_atr": self.risk.level_distance_atr,
                "nearest_level_id": self.risk.nearest_level_id,
                "reasons": list(self.risk.reasons),
                "action": self.risk.action.value,
            },
            "mode": self.mode,
        }


@dataclass(frozen=True)
class LiquidityConfig:
    pivot_left: int = 3
    pivot_right: int = 2
    equal_level_tolerance_bps: float = 8.0
    min_equal_touches: int = 2
    max_levels: int = 80
    sweep_min_penetration_atr: float = 0.03
    sweep_max_penetration_atr: float = 1.5
    reclaim_bars: int = 3
    acceptance_bars: int = 2
    minimum_confirmation_confidence: float = 0.60
    risk_nearby_atr: float = 1.0
    risk_block_threshold: float = 78.0
    stop_buffer_atr: float = 0.20
    min_stop_buffer_bps: float = 5.0
    max_stop_distance_pct: float = 0.04

    def __post_init__(self) -> None:
        if self.pivot_left < 1 or self.pivot_right < 1:
            raise ValueError("pivot windows must be positive")
        if self.min_equal_touches < 2 or self.max_levels < 1:
            raise ValueError("invalid liquidity level limits")
        if self.reclaim_bars < 1 or self.acceptance_bars < 1:
            raise ValueError("confirmation windows must be positive")
        if not 0 < self.risk_block_threshold <= 100:
            raise ValueError("risk threshold must be in (0, 100]")
        if not 0 < self.max_stop_distance_pct < 1:
            raise ValueError("max stop distance must be in (0, 1)")


def _atr(candles: Sequence[Candle], period: int = 14) -> float:
    if not candles:
        return 0.0
    ranges = []
    previous_close = candles[0].close
    for candle in candles[-period:]:
        ranges.append(max(
            candle.high - candle.low,
            abs(candle.high - previous_close),
            abs(candle.low - previous_close),
        ))
        previous_close = candle.close
    return mean(ranges) if ranges else 0.0


def _volume_zscore(candles: Sequence[Candle], index: int, window: int = 20) -> float:
    history = [c.volume for c in candles[max(0, index - window):index]]
    if len(history) < 5:
        return 0.0
    deviation = pstdev(history)
    if deviation <= 1e-12:
        return 0.0
    return (candles[index].volume - mean(history)) / deviation


def map_liquidity_levels(
    symbol: str,
    timeframe: str,
    raw_candles: Sequence[Any],
    *,
    config: LiquidityConfig | None = None,
    reference_levels: Iterable[Mapping[str, Any]] = (),
) -> tuple[LiquidityLevel, ...]:
    """Map confirmed swing and equal-high/low pools from completed candles.

    Pivots are only emitted after pivot_right bars have completed, avoiding
    look-ahead. External references (e.g. previous day/week/session H/L) must
    be supplied by a point-in-time caller with their source and timestamp.
    """
    cfg = config or LiquidityConfig()
    candles = [Candle.from_value(item) for item in raw_candles if Candle.from_value(item).is_closed]
    if len(candles) < cfg.pivot_left + cfg.pivot_right + 1:
        return ()
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for idx in range(cfg.pivot_left, len(candles) - cfg.pivot_right):
        item = candles[idx]
        left = candles[idx - cfg.pivot_left:idx]
        right = candles[idx + 1:idx + cfg.pivot_right + 1]
        if all(item.high > x.high for x in left) and all(item.high >= x.high for x in right):
            highs.append((idx, item.high))
        if all(item.low < x.low for x in left) and all(item.low <= x.low for x in right):
            lows.append((idx, item.low))

    levels: list[LiquidityLevel] = []

    def add_cluster(points: list[tuple[int, float]], side: LiquiditySide, source: LiquiditySource) -> None:
        unused = set(range(len(points)))
        while unused:
            first = min(unused)
            base = points[first][1]
            cluster = [i for i in sorted(unused) if abs(points[i][1] - base) / base * 10000 <= cfg.equal_level_tolerance_bps]
            for i in cluster:
                unused.discard(i)
            if len(cluster) < cfg.min_equal_touches:
                continue
            prices = [points[i][1] for i in cluster]
            price = mean(prices)
            touch_count = len(cluster)
            strength = min(100.0, 25.0 + touch_count * 12.0 + min(20.0, max(prices) - min(prices)) / price * 10000)
            timestamps = [candles[points[i][0]].timestamp for i in cluster]
            levels.append(LiquidityLevel(
                level_id=uuid.uuid4().hex,
                symbol=symbol.upper(),
                side=side,
                price=price,
                source=source,
                timeframe=timeframe,
                strength=round(strength, 2),
                touch_count=touch_count,
                first_seen=min(timestamps) if timestamps else "",
                last_seen=max(timestamps) if timestamps else "",
                evidence=(f"{touch_count} confirmed pivots within {cfg.equal_level_tolerance_bps:g} bps",),
            ))

    add_cluster(highs, LiquiditySide.BUY_SIDE, LiquiditySource.EQUAL_HIGHS)
    add_cluster(lows, LiquiditySide.SELL_SIDE, LiquiditySource.EQUAL_LOWS)
    for idx, price in highs:
        if not any(abs(level.price - price) / price * 10000 <= cfg.equal_level_tolerance_bps for level in levels if level.side is LiquiditySide.BUY_SIDE):
            levels.append(LiquidityLevel(uuid.uuid4().hex, symbol.upper(), LiquiditySide.BUY_SIDE, price, LiquiditySource.SWING_HIGH, timeframe, 35.0, 1, candles[idx].timestamp, candles[idx].timestamp, ("confirmed swing high",)))
    for idx, price in lows:
        if not any(abs(level.price - price) / price * 10000 <= cfg.equal_level_tolerance_bps for level in levels if level.side is LiquiditySide.SELL_SIDE):
            levels.append(LiquidityLevel(uuid.uuid4().hex, symbol.upper(), LiquiditySide.SELL_SIDE, price, LiquiditySource.SWING_LOW, timeframe, 35.0, 1, candles[idx].timestamp, candles[idx].timestamp, ("confirmed swing low",)))

    for raw in reference_levels:
        try:
            side = LiquiditySide(str(raw["side"]).lower())
            source = LiquiditySource(str(raw.get("source", "external_reference")))
            price = float(raw["price"])
            if not isfinite(price) or price <= 0:
                continue
            levels.append(LiquidityLevel(
                level_id=str(raw.get("level_id") or uuid.uuid4().hex),
                symbol=symbol.upper(),
                side=side,
                price=price,
                source=source,
                timeframe=str(raw.get("timeframe") or timeframe),
                strength=max(0.0, min(100.0, float(raw.get("strength", 60.0)))),
                touch_count=max(1, int(raw.get("touch_count", 1))),
                first_seen=str(raw.get("first_seen", "")),
                last_seen=str(raw.get("last_seen", "")),
                evidence=tuple(raw.get("evidence", ("point-in-time reference level",))),
            ))
        except (KeyError, TypeError, ValueError):
            continue

    # Stable deduplication by side and price; strongest evidence wins.
    deduped: dict[tuple[str, int], LiquidityLevel] = {}
    for level in levels:
        key = (level.side.value, round(level.price / max(level.price * cfg.equal_level_tolerance_bps / 10000, 1e-12)))
        previous = deduped.get(key)
        if previous is None or level.strength > previous.strength:
            deduped[key] = level
    return tuple(sorted(deduped.values(), key=lambda x: (-x.strength, x.price))[:cfg.max_levels])


def detect_liquidity_sweeps(
    levels: Sequence[LiquidityLevel],
    raw_candles: Sequence[Any],
    *,
    config: LiquidityConfig | None = None,
) -> tuple[SweepEvent, ...]:
    cfg = config or LiquidityConfig()
    candles = [Candle.from_value(item) for item in raw_candles if Candle.from_value(item).is_closed]
    events: list[SweepEvent] = []
    for idx, candle in enumerate(candles):
        atr = _atr(candles[:idx + 1])
        if atr <= 0:
            continue
        for level in levels:
            if level.side is LiquiditySide.SELL_SIDE:
                penetration = level.price - candle.low
                reclaimed = candle.close > level.price
                direction = "down"
                wick = max(0.0, min(1.0, (min(candle.open, candle.close) - candle.low) / max(candle.high - candle.low, 1e-12)))
            else:
                penetration = candle.high - level.price
                reclaimed = candle.close < level.price
                direction = "up"
                wick = max(0.0, min(1.0, (candle.high - max(candle.open, candle.close)) / max(candle.high - candle.low, 1e-12)))
            depth_atr = penetration / atr
            if penetration <= 0 or depth_atr < cfg.sweep_min_penetration_atr or depth_atr > cfg.sweep_max_penetration_atr:
                continue
            events.append(SweepEvent(
                event_id=f"{level.level_id}:{idx}",
                level=level,
                direction=direction,
                sweep_price=candle.low if direction == "down" else candle.high,
                penetration=penetration,
                candle_index=idx,
                candle_timestamp=candle.timestamp,
                wick_ratio=wick,
                volume_zscore=_volume_zscore(candles, idx),
                state=SweepState.SWEPT if not reclaimed else SweepState.REJECTION_PENDING,
                evidence=("level penetrated", "completed candle only") + (("same-candle reclaim",) if reclaimed else ()),
            ))
    return tuple(events)


def classify_sweep(
    event: SweepEvent,
    raw_candles: Sequence[Any],
    *,
    flow: OrderFlowEvidence | None = None,
    config: LiquidityConfig | None = None,
) -> SweepClassification:
    cfg = config or LiquidityConfig()
    candles = [Candle.from_value(item) for item in raw_candles if Candle.from_value(item).is_closed]
    future = candles[event.candle_index + 1:event.candle_index + 1 + max(cfg.reclaim_bars, cfg.acceptance_bars)]
    flow = flow or OrderFlowEvidence()
    evidence = list(event.evidence)
    if not future:
        return SweepClassification(SweepState.INDECISION, LiquidityAction.WAIT, 0.0, ("awaiting post-sweep completed bars",), 0)

    level = event.level.price
    if event.direction == "down":
        reclaimed = any(c.close > level for c in future[:cfg.reclaim_bars])
        accepted = len(future) >= cfg.acceptance_bars and all(c.close < level for c in future[:cfg.acceptance_bars])
        flow_confirms = flow.fresh and flow.confidence >= 0.5 and (flow.cvd_score >= 6.0 or flow.ofi_score >= 6.0 or flow.bid_replenishment > 0)
        if reclaimed and flow_confirms:
            evidence.extend(("sell-side liquidity reclaimed", "bullish order-flow confirmation"))
            return SweepClassification(SweepState.REJECTED, LiquidityAction.REVERSAL_LONG, min(1.0, 0.55 + flow.confidence * 0.35 + event.wick_ratio * 0.1), tuple(evidence), len(future))
        if accepted:
            evidence.append("price accepted below sell-side liquidity")
            return SweepClassification(SweepState.ACCEPTED, LiquidityAction.CONTINUATION_SHORT, 0.65, tuple(evidence), len(future))
    else:
        reclaimed = any(c.close < level for c in future[:cfg.reclaim_bars])
        accepted = len(future) >= cfg.acceptance_bars and all(c.close > level for c in future[:cfg.acceptance_bars])
        flow_confirms = flow.fresh and flow.confidence >= 0.5 and (flow.cvd_score <= 4.0 or flow.ofi_score <= 4.0 or flow.ask_replenishment > 0)
        if reclaimed and flow_confirms:
            evidence.extend(("buy-side liquidity reclaimed", "bearish order-flow confirmation"))
            return SweepClassification(SweepState.REJECTED, LiquidityAction.REVERSAL_SHORT, min(1.0, 0.55 + flow.confidence * 0.35 + event.wick_ratio * 0.1), tuple(evidence), len(future))
        if accepted:
            evidence.append("price accepted above buy-side liquidity")
            return SweepClassification(SweepState.ACCEPTED, LiquidityAction.CONTINUATION_LONG, 0.65, tuple(evidence), len(future))
    evidence.append("post-sweep evidence is mixed or incomplete")
    return SweepClassification(SweepState.INDECISION, LiquidityAction.WAIT, 0.25, tuple(evidence), len(future))


def score_liquidity_risk(
    price: float,
    levels: Sequence[LiquidityLevel],
    *,
    atr: float,
    direction: str,
    flow: OrderFlowEvidence | None = None,
    volatility_expansion: float = 0.0,
    config: LiquidityConfig | None = None,
) -> LiquidityRisk:
    cfg = config or LiquidityConfig()
    flow = flow or OrderFlowEvidence()
    if not isfinite(price) or price <= 0:
        raise ValueError("price must be positive and finite")
    relevant = [x for x in levels if (direction.upper() == "LONG" and x.side is LiquiditySide.SELL_SIDE and x.price < price) or (direction.upper() == "SHORT" and x.side is LiquiditySide.BUY_SIDE and x.price > price)]
    nearest = min(relevant, key=lambda x: abs(price - x.price)) if relevant else None
    distance = abs(price - nearest.price) / atr if nearest is not None and atr > 0 else None
    proximity = 0.0 if distance is None else max(0.0, 1.0 - distance / max(cfg.risk_nearby_atr, 1e-9)) * 35.0
    strength = (nearest.strength / 100.0 * 25.0) if nearest else 0.0
    adverse_flow = 0.0
    reasons: list[str] = []
    if nearest:
        reasons.append("nearby_" + nearest.source.value)
    if flow.fresh and flow.confidence >= 0.5:
        adverse_flow = (max(0.0, 5.0 - flow.cvd_score) + max(0.0, 5.0 - flow.ofi_score)) * 2.0 if direction.upper() == "LONG" else (max(0.0, flow.cvd_score - 5.0) + max(0.0, flow.ofi_score - 5.0)) * 2.0
        adverse_flow = min(25.0, adverse_flow)
        if adverse_flow >= 8.0:
            reasons.append("fresh_adverse_order_flow")
    vol_score = max(0.0, min(15.0, float(volatility_expansion) * 5.0))
    if vol_score >= 5:
        reasons.append("volatility_expansion")
    score = round(max(0.0, min(100.0, proximity + strength + adverse_flow + vol_score)), 2)
    action = LiquidityAction.BLOCK_ENTRY if score >= cfg.risk_block_threshold else (LiquidityAction.WAIT if score >= cfg.risk_block_threshold * 0.65 else LiquidityAction.SHADOW)
    return LiquidityRisk(score, distance, nearest.level_id if nearest else None, tuple(reasons), action)


def plan_liquidity_aware_stop(
    *,
    side: str,
    entry_price: float,
    invalidation_price: float,
    atr: float,
    config: LiquidityConfig | None = None,
) -> StopPlan:
    cfg = config or LiquidityConfig()
    normalized = side.upper()
    if normalized not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    if not all(isfinite(x) and x > 0 for x in (entry_price, invalidation_price)):
        raise ValueError("entry and invalidation prices must be positive and finite")
    if normalized == "LONG" and invalidation_price >= entry_price:
        raise ValueError("LONG invalidation must be below entry")
    if normalized == "SHORT" and invalidation_price <= entry_price:
        raise ValueError("SHORT invalidation must be above entry")
    buffer = max(entry_price * cfg.min_stop_buffer_bps / 10000.0, max(0.0, atr) * cfg.stop_buffer_atr)
    stop = invalidation_price - buffer if normalized == "LONG" else invalidation_price + buffer
    distance = abs(entry_price - stop)
    distance_pct = distance / entry_price
    allowed = distance_pct <= cfg.max_stop_distance_pct
    return StopPlan(normalized, entry_price, invalidation_price, stop, distance, distance_pct, buffer, allowed, "thesis_invalidation_with_liquidity_buffer" if allowed else "stop_exceeds_max_distance_reduce_size_or_reject")


def position_quantity_for_risk(
    *, equity: float, risk_fraction: float, stop_distance: float, contract_multiplier: float = 1.0,
    estimated_cost_per_unit: float = 0.0,
) -> float:
    """Risk-budget sizing; costs are reserved before quantity is calculated."""
    values = (equity, risk_fraction, stop_distance, contract_multiplier, estimated_cost_per_unit)
    if not all(isfinite(float(x)) for x in values):
        raise ValueError("sizing inputs must be finite")
    if equity <= 0 or not 0 < risk_fraction <= 1 or stop_distance <= 0 or contract_multiplier <= 0 or estimated_cost_per_unit < 0:
        raise ValueError("invalid sizing inputs")
    budget = equity * risk_fraction
    per_unit_risk = stop_distance * contract_multiplier + estimated_cost_per_unit
    return max(0.0, budget / per_unit_risk)


def analyze_liquidity(
    symbol: str,
    timeframe: str,
    raw_candles: Sequence[Any],
    *,
    direction: str = "LONG",
    flow: OrderFlowEvidence | None = None,
    reference_levels: Iterable[Mapping[str, Any]] = (),
    config: LiquidityConfig | None = None,
) -> LiquidityAnalysis:
    cfg = config or LiquidityConfig()
    candles = [Candle.from_value(item) for item in raw_candles if Candle.from_value(item).is_closed]
    levels = map_liquidity_levels(symbol, timeframe, candles, config=cfg, reference_levels=reference_levels)
    sweeps = detect_liquidity_sweeps(levels, candles, config=cfg)
    classification = classify_sweep(sweeps[-1], candles, flow=flow, config=cfg) if sweeps else None
    atr = _atr(candles)
    risk = score_liquidity_risk(candles[-1].close, levels, atr=atr, direction=direction, flow=flow, config=cfg) if candles else LiquidityRisk(0.0, None, None, ("insufficient_candles",), LiquidityAction.WAIT)
    return LiquidityAnalysis(symbol.upper(), timeframe, levels, sweeps, classification, risk)


@dataclass(frozen=True)
class LiquidityOutcome:
    event_id: str
    symbol: str
    direction: str
    decision: str
    realized_pnl: float
    mae: float
    mfe: float
    fees: float = 0.0
    funding: float = 0.0
    slippage: float = 0.0

    @property
    def net_pnl(self) -> float:
        return self.realized_pnl - self.fees - self.slippage + self.funding


def attribute_liquidity_outcome(outcome: LiquidityOutcome) -> dict[str, Any]:
    if not all(isfinite(float(x)) for x in (outcome.realized_pnl, outcome.mae, outcome.mfe, outcome.fees, outcome.funding, outcome.slippage)):
        raise ValueError("outcome metrics must be finite")
    return {
        "event_id": outcome.event_id,
        "symbol": outcome.symbol.upper(),
        "direction": outcome.direction.upper(),
        "decision": outcome.decision,
        "gross_pnl": outcome.realized_pnl,
        "net_pnl": outcome.net_pnl,
        "mae": outcome.mae,
        "mfe": outcome.mfe,
        "fees": outcome.fees,
        "funding": outcome.funding,
        "slippage": outcome.slippage,
        "profitable": outcome.net_pnl > 0,
    }
