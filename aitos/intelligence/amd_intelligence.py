"""Evidence-based Accumulation-Manipulation-Distribution intelligence.

AMD is an advisory market-context classifier, not a trading strategy. It reuses
AITOS candle/liquidity/order-flow contracts and never claims to observe hidden
stop orders or participant intent. Scores are heuristic research features, not
calibrated probabilities.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import Enum
from hashlib import sha256
from statistics import mean
from typing import Any, Sequence

from aitos.intelligence.liquidity_intelligence import (
    Candle,
    LiquidityAction,
    LiquidityAnalysis,
    LiquiditySide,
    OrderFlowEvidence,
    SweepState,
)


class AMDPhase(str, Enum):
    NEUTRAL = "neutral"
    ACCUMULATION_CANDIDATE = "accumulation_candidate"
    ACCUMULATION_CONFIRMED = "accumulation_confirmed"
    MANIPULATION_CANDIDATE = "manipulation_candidate"
    MANIPULATION_CONFIRMED = "manipulation_confirmed"
    DISTRIBUTION_CANDIDATE = "distribution_candidate"
    DISTRIBUTION_CONFIRMED = "distribution_confirmed"
    REACCUMULATION = "reaccumulation"
    REDISTRIBUTION = "redistribution"
    INVALIDATED = "invalidated"


@dataclass(frozen=True)
class AMDConfig:
    window: int = 30
    min_window: int = 20
    range_atr_max: float = 6.0
    compression_ratio_max: float = 0.85
    min_liquidity_pools: int = 1
    min_accumulation_score: float = 0.55
    confirmed_accumulation_score: float = 0.70
    min_distribution_score: float = 0.65
    volume_expansion_ratio: float = 1.25
    min_flow_confidence: float = 0.60

    def __post_init__(self) -> None:
        if self.window < self.min_window or self.min_window < 8:
            raise ValueError("AMD window must be >= min_window >= 8")
        if self.range_atr_max <= 0 or self.compression_ratio_max <= 0:
            raise ValueError("range and compression limits must be positive")
        for name in (
            "min_accumulation_score",
            "confirmed_accumulation_score",
            "min_distribution_score",
            "min_flow_confidence",
        ):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(name + " must be in [0, 1]")
        if self.volume_expansion_ratio <= 0 or self.min_liquidity_pools < 1:
            raise ValueError("volume and liquidity-pool limits must be positive")


@dataclass(frozen=True)
class AMDState:
    symbol: str
    timeframe: str
    phase: AMDPhase
    direction: str
    state_id: str
    as_of: str
    accumulation_score: float
    manipulation_score: float
    distribution_score: float
    confidence: float
    evidence_coverage: float
    range_high: float | None
    range_low: float | None
    liquidity_event_id: str | None
    target_liquidity_ids: tuple[str, ...]
    evidence: tuple[str, ...]
    invalidation_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["phase"] = self.phase.value
        value["target_liquidity_ids"] = list(self.target_liquidity_ids)
        value["evidence"] = list(self.evidence)
        return value


@dataclass(frozen=True)
class _AccumulationFeatures:
    score: float
    coverage: float
    range_high: float
    range_low: float
    range_atr: float
    compression_ratio: float | None
    volume_ratio: float | None
    liquidity_pool_count: int
    evidence: tuple[str, ...]


def _stable_id(*parts: Any) -> str:
    raw = "|".join(str(part) for part in parts).encode("utf-8")
    return sha256(raw).hexdigest()[:24]


def _closed_candles(raw: Sequence[Any]) -> list[Candle]:
    rows = [Candle.from_value(item) for item in raw]
    return [row for row in rows if row.is_closed]


def _atr(rows: Sequence[Candle]) -> float:
    if len(rows) < 2:
        return 0.0
    values: list[float] = []
    previous = rows[0].close
    for row in rows[1:]:
        values.append(
            max(
                row.high - row.low,
                abs(row.high - previous),
                abs(row.low - previous),
            )
        )
        previous = row.close
    return mean(values) if values else 0.0


def _accumulation_features(
    symbol: str,
    timeframe: str,
    rows: Sequence[Candle],
    liquidity: LiquidityAnalysis | None,
    flow: OrderFlowEvidence,
    config: AMDConfig,
) -> _AccumulationFeatures | None:
    if len(rows) < config.min_window:
        return None
    window = list(rows[-config.window :])
    high = max(row.high for row in window)
    low = min(row.low for row in window)
    atr = _atr(window)
    if atr <= 0 or high <= low:
        return None

    range_atr = (high - low) / atr
    recent = window[-max(5, len(window) // 3) :]
    prior = window[: max(5, len(window) - len(recent))]
    recent_atr = _atr(recent)
    prior_atr = _atr(prior)
    compression = recent_atr / prior_atr if prior_atr > 0 else None
    recent_volume = mean(row.volume for row in recent)
    prior_volume = mean(row.volume for row in prior) if prior else 0.0
    volume_ratio = recent_volume / prior_volume if prior_volume > 0 else None

    pools = [
        level
        for level in (liquidity.levels if liquidity else ())
        if level.symbol.upper() == symbol.upper()
        and level.timeframe == timeframe
        and low <= level.price <= high
    ]
    evidence: list[str] = []
    components: list[tuple[float, float]] = []

    # A tight range and contracting true range support balance; neither proves
    # institutional accumulation by itself.
    range_score = max(0.0, min(1.0, 1.0 - range_atr / config.range_atr_max))
    components.append((range_score, 0.25))
    evidence.append(
        "completed_candle_range_compression"
        if range_score > 0.2
        else "range_not_compressed"
    )

    if compression is not None:
        compression_score = max(
            0.0,
            min(
                1.0,
                (config.compression_ratio_max - compression)
                / config.compression_ratio_max,
            ),
        )
        components.append((compression_score, 0.20))
        evidence.append(
            "true_range_contracting"
            if compression < config.compression_ratio_max
            else "no_volatility_contraction"
        )

    if pools:
        pool_score = min(
            1.0, len(pools) / max(1, config.min_liquidity_pools * 2)
        )
        components.append((pool_score, 0.20))
        evidence.append("liquidity_pools_inside_range")
    else:
        evidence.append("no_mapped_internal_liquidity_pool")

    if volume_ratio is not None:
        # Lower activity is a weak balance feature; volume can also rise during
        # absorption, so volume alone must not decide the phase.
        volume_score = max(0.0, min(1.0, (1.5 - volume_ratio)))
        components.append((volume_score, 0.10))
        evidence.append(
            "range_volume_contracting"
            if volume_ratio < 1.0
            else "range_volume_not_contracting"
        )

    if flow.fresh and flow.confidence >= config.min_flow_confidence:
        replenishment = min(
            1.0, (flow.bid_replenishment + flow.ask_replenishment) / 10.0
        )
        components.append((replenishment, 0.15))
        evidence.append(
            "fresh_two_sided_liquidity_replenishment"
            if replenishment > 0
            else "fresh_flow_without_replenishment"
        )
    else:
        evidence.append("fresh_order_flow_unavailable")

    # Normalize over observed components and report coverage separately.
    weight = sum(item[1] for item in components)
    score = (
        sum(value * weight_ for value, weight_ in components) / weight
        if weight
        else 0.0
    )
    coverage = min(1.0, weight / 0.90)
    return _AccumulationFeatures(
        round(score, 4),
        round(coverage, 4),
        high,
        low,
        round(range_atr, 4),
        round(compression, 4) if compression is not None else None,
        round(volume_ratio, 4) if volume_ratio is not None else None,
        len(pools),
        tuple(evidence),
    )


def analyze_amd(
    symbol: str,
    timeframe: str,
    raw_candles: Sequence[Any],
    *,
    liquidity: LiquidityAnalysis | None = None,
    flow: OrderFlowEvidence | None = None,
    structure_bias: str | None = None,
    previous_state: AMDState | None = None,
    config: AMDConfig | None = None,
) -> AMDState:
    """Classify the current AMD phase from closed candles and supplied evidence.

    A durable caller may pass a previously persisted checkpoint. Otherwise the
    phase is deterministically reconstructed from the bounded candle window
    and current evidence; the engine itself has no hidden mutable state.
    """
    cfg = config or AMDConfig()
    order_flow = flow or OrderFlowEvidence()
    rows = _closed_candles(raw_candles)
    if not rows:
        raise ValueError("AMD requires at least one completed candle")
    latest = rows[-1]
    as_of = latest.timestamp or ""
    accumulation = _accumulation_features(
        symbol, timeframe, rows, liquidity, order_flow, cfg
    )

    acc_score = accumulation.score if accumulation else 0.0
    acc_coverage = accumulation.coverage if accumulation else 0.0
    acc_evidence = list(
        accumulation.evidence
        if accumulation
        else ("insufficient_closed_candles",)
    )
    sweeps = liquidity.sweeps if liquidity else ()
    classification = liquidity.latest_classification if liquidity else None
    latest_sweep = sweeps[-1] if sweeps else None

    manipulation_score = 0.0
    manipulation_evidence: list[str] = []
    if latest_sweep is not None:
        manipulation_score += 0.30
        manipulation_evidence.append("mapped_liquidity_level_penetrated")
        if classification is not None:
            if classification.state is SweepState.REJECTED:
                manipulation_score += 0.45
                manipulation_evidence.append(
                    "post_sweep_reclaim_classified_rejected"
                )
            elif classification.state is SweepState.ACCEPTED:
                manipulation_score += 0.35
                manipulation_evidence.append("post_sweep_price_acceptance")
            else:
                manipulation_evidence.append(
                    "post_sweep_confirmation_pending"
                )
        if (
            order_flow.fresh
            and order_flow.confidence >= cfg.min_flow_confidence
        ):
            manipulation_score += 0.25
            manipulation_evidence.append("fresh_order_flow_snapshot")
    manipulation_score = min(1.0, manipulation_score)

    direction = ""
    if classification is not None:
        if classification.action in {
            LiquidityAction.REVERSAL_LONG,
            LiquidityAction.CONTINUATION_LONG,
        }:
            direction = "LONG"
        elif classification.action in {
            LiquidityAction.REVERSAL_SHORT,
            LiquidityAction.CONTINUATION_SHORT,
        }:
            direction = "SHORT"

    normalized_structure = str(structure_bias or "").strip().lower()
    if normalized_structure not in {"bullish", "bearish"}:
        normalized_structure = ""
    if not normalized_structure and len(rows) >= 4:
        closes = [row.close for row in rows[-4:]]
        if closes[-1] > closes[0] and closes[-1] > closes[-2]:
            normalized_structure = "bullish"
        elif closes[-1] < closes[0] and closes[-1] < closes[-2]:
            normalized_structure = "bearish"

    recent = rows[-5:]
    prior = rows[-10:-5]
    recent_volume = mean(row.volume for row in recent)
    prior_volume = mean(row.volume for row in prior) if prior else 0.0
    volume_expansion = recent_volume / prior_volume if prior_volume > 0 else None
    structure_matches = bool(
        direction
        and normalized_structure
        and normalized_structure == ("bullish" if direction == "LONG" else "bearish")
    )
    flow_matches = bool(
        direction
        and order_flow.fresh
        and order_flow.confidence >= cfg.min_flow_confidence
        and (
            (
                direction == "LONG"
                and order_flow.cvd_score >= 6.0
                and order_flow.ofi_score >= 5.0
            )
            or (
                direction == "SHORT"
                and order_flow.cvd_score <= 4.0
                and order_flow.ofi_score <= 5.0
            )
        )
    )
    expansion_matches = bool(
        volume_expansion is not None
        and volume_expansion >= cfg.volume_expansion_ratio
    )
    distribution_parts = [structure_matches, flow_matches, expansion_matches]
    distribution_score = sum(1.0 for item in distribution_parts if item) / 3.0
    distribution_evidence = [
        name
        for name, passed in (
            ("directional_structure_confirmation", structure_matches),
            ("fresh_directional_cvd_ofi_confirmation", flow_matches),
            ("relative_volume_expansion", expansion_matches),
        )
        if passed
    ]
    if volume_expansion is None:
        distribution_evidence.append("volume_expansion_unavailable")

    manipulation_confirmed = bool(
        classification is not None
        and classification.state is SweepState.REJECTED
        and latest_sweep is not None
        and order_flow.fresh
        and order_flow.confidence >= cfg.min_flow_confidence
    )

    if (
        distribution_score >= cfg.min_distribution_score
        and direction
        and structure_matches
        and flow_matches
        and expansion_matches
    ):
        phase = AMDPhase.DISTRIBUTION_CONFIRMED
    elif direction and any(distribution_parts):
        phase = AMDPhase.DISTRIBUTION_CANDIDATE
    elif manipulation_confirmed:
        phase = AMDPhase.MANIPULATION_CONFIRMED
    elif latest_sweep is not None:
        phase = AMDPhase.MANIPULATION_CANDIDATE
    elif (
        accumulation is not None
        and acc_score >= cfg.confirmed_accumulation_score
        and acc_coverage >= 0.60
        and accumulation.liquidity_pool_count >= cfg.min_liquidity_pools
    ):
        phase = AMDPhase.ACCUMULATION_CONFIRMED
    elif accumulation is not None and acc_score >= cfg.min_accumulation_score:
        phase = AMDPhase.ACCUMULATION_CANDIDATE
    else:
        phase = AMDPhase.NEUTRAL

    if previous_state is not None and phase is AMDPhase.ACCUMULATION_CONFIRMED:
        if previous_state.direction == "LONG":
            phase = AMDPhase.REACCUMULATION
        elif previous_state.direction == "SHORT":
            phase = AMDPhase.REDISTRIBUTION

    evidence = acc_evidence + manipulation_evidence + distribution_evidence
    if normalized_structure:
        evidence.append("structure_bias_" + normalized_structure)
    event_id = latest_sweep.event_id if latest_sweep else "no-sweep"
    state_id = _stable_id(
        symbol.upper(), timeframe, as_of, phase.value, event_id
    )
    coverage_values = [acc_coverage]
    if latest_sweep is not None:
        coverage_values.append(1.0 if classification is not None else 0.5)
    if direction:
        coverage_values.append(
            sum(
                (
                    normalized_structure != "",
                    order_flow.fresh,
                    volume_expansion is not None,
                )
            )
            / 3.0
        )
    coverage = round(sum(coverage_values) / len(coverage_values), 4)
    confidence = round(
        max(
            acc_score
            if phase
            in {
                AMDPhase.ACCUMULATION_CANDIDATE,
                AMDPhase.ACCUMULATION_CONFIRMED,
                AMDPhase.REACCUMULATION,
                AMDPhase.REDISTRIBUTION,
            }
            else 0.0,
            manipulation_score
            if phase
            in {
                AMDPhase.MANIPULATION_CANDIDATE,
                AMDPhase.MANIPULATION_CONFIRMED,
            }
            else 0.0,
            distribution_score
            if phase
            in {
                AMDPhase.DISTRIBUTION_CANDIDATE,
                AMDPhase.DISTRIBUTION_CONFIRMED,
            }
            else 0.0,
        ),
        4,
    )
    targets = tuple(
        level.level_id
        for level in (liquidity.levels if liquidity else ())
        if (
            direction == "LONG"
            and level.side is LiquiditySide.BUY_SIDE
            and level.price > latest.close
        )
        or (
            direction == "SHORT"
            and level.side is LiquiditySide.SELL_SIDE
            and level.price < latest.close
        )
    )
    return AMDState(
        symbol=symbol.upper(),
        timeframe=timeframe,
        phase=phase,
        direction=direction,
        state_id=state_id,
        as_of=as_of,
        accumulation_score=round(acc_score, 4),
        manipulation_score=round(manipulation_score, 4),
        distribution_score=round(distribution_score, 4),
        confidence=confidence,
        evidence_coverage=coverage,
        range_high=accumulation.range_high if accumulation else None,
        range_low=accumulation.range_low if accumulation else None,
        liquidity_event_id=latest_sweep.event_id if latest_sweep else None,
        target_liquidity_ids=targets,
        evidence=tuple(dict.fromkeys(evidence)),
    )


@dataclass(frozen=True)
class AMDTimeframeStack:
    symbol: str
    direction: str
    bullish_score: float
    bearish_score: float
    available_timeframes: tuple[str, ...]
    missing_timeframes: tuple[str, ...]
    stack_complete: bool
    evidence_coverage: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_AMD_TIMEFRAME_WEIGHTS = {
    "1d": 2.0,
    "4h": 1.8,
    "1h": 1.0,
    "15m": 0.8,
    "5m": 0.6,
    "1m": 0.4,
}


def analyze_amd_timeframe_stack(
    states: Mapping[str, AMDState],
    *,
    required_timeframes: Sequence[str] = ("1d", "4h", "1h", "15m", "5m", "1m"),
    neutral_margin: float = 0.10,
) -> AMDTimeframeStack:
    """Aggregate only supplied AMD states; missing frames remain explicitly missing.

    The role weights follow AITOS MTF hierarchy. A single available timeframe
    is not promoted to a complete stack and cannot silently stand in for HTF.
    """
    normalized = {str(key).lower(): value for key, value in states.items()}
    symbols = {value.symbol.upper() for value in normalized.values()}
    if len(symbols) > 1:
        raise ValueError("AMD timeframe stack cannot mix symbols")
    symbol = next(iter(symbols), "")
    available = tuple(
        sorted(
            key
            for key, value in normalized.items()
            if value.timeframe.lower() == key
        )
    )
    required = tuple(str(item).lower() for item in required_timeframes)
    missing = tuple(item for item in required if item not in normalized)
    bullish = 0.0
    bearish = 0.0
    total_weight = 0.0
    for timeframe, state in normalized.items():
        weight = _AMD_TIMEFRAME_WEIGHTS.get(timeframe, 0.25)
        total_weight += weight
        active = state.phase in {
            AMDPhase.MANIPULATION_CANDIDATE,
            AMDPhase.MANIPULATION_CONFIRMED,
            AMDPhase.DISTRIBUTION_CANDIDATE,
            AMDPhase.DISTRIBUTION_CONFIRMED,
        }
        if not active:
            continue
        contribution = weight * max(0.0, min(1.0, state.confidence))
        if state.direction == "LONG":
            bullish += contribution
        elif state.direction == "SHORT":
            bearish += contribution
    if total_weight:
        bullish /= total_weight
        bearish /= total_weight
    if bullish - bearish > neutral_margin:
        direction = "LONG"
    elif bearish - bullish > neutral_margin:
        direction = "SHORT"
    else:
        direction = "NEUTRAL"
    coverage = (
        sum(_AMD_TIMEFRAME_WEIGHTS.get(item, 0.25) for item in available)
        / sum(_AMD_TIMEFRAME_WEIGHTS.get(item, 0.25) for item in required)
        if required
        else 1.0
    )
    return AMDTimeframeStack(
        symbol=symbol,
        direction=direction,
        bullish_score=round(bullish, 4),
        bearish_score=round(bearish, 4),
        available_timeframes=available,
        missing_timeframes=missing,
        stack_complete=not missing,
        evidence_coverage=round(min(1.0, coverage), 4),
    )
