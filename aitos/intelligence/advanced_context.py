"""Advanced, quantifiable market-context features for AITOS.

This module deliberately avoids turning ICT/SMC vocabulary into hard trading
rules.  It exposes measurable observations that can be consumed by the AI
context layer: volume-profile location, volatility regime, price imbalance,
liquidation/forced-flow proxies, and normalized left/right structural
symmetry.  Every feature is optional and returns a neutral/unknown state when
its required evidence is unavailable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from math import isfinite
from statistics import mean, pstdev

from aitos.models.market import Kline


@dataclass(frozen=True)
class VolumeProfileContext:
    poc: float
    vah: float
    val: float
    hvn: tuple[float, ...]
    lvn: tuple[float, ...]
    price_location: float  # 0=VAL, 0.5=POC, 1=VAH (clamped)
    acceptance_score: float  # 0..1


@dataclass(frozen=True)
class VolatilityContext:
    atr: float
    percentile: float
    regime: str  # compression | normal | expansion | extreme
    expansion_rate: float


@dataclass(frozen=True)
class ImbalanceContext:
    zones: tuple[tuple[float, float], ...]
    nearest_above: float | None
    nearest_below: float | None
    displacement_score: float


@dataclass(frozen=True)
class SymmetryMatch:
    similarity: float
    scale: float
    time_scale: float
    projected_levels: tuple[float, ...]
    mirrored: bool
    failure_distance: float


@dataclass(frozen=True)
class AdvancedMarketContext:
    volume_profile: VolumeProfileContext | None
    volatility: VolatilityContext
    imbalance: ImbalanceContext
    symmetry: SymmetryMatch | None
    forced_flow_score: float
    features: dict[str, float] = field(default_factory=dict)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def volume_profile(
    klines: Sequence[Kline], bins: int = 24, value_area_pct: float = 0.70
) -> VolumeProfileContext | None:
    """Approximate a volume-at-price profile from OHLCV bars.

    Without tick-level historical volume, each candle's volume is distributed
    uniformly across its high/low range.  This is intentionally an
    approximation; live footprint data remains the higher-resolution source.
    """
    if not klines or bins < 4 or not 0.5 <= value_area_pct <= 0.95:
        return None
    low = min(k.low for k in klines)
    high = max(k.high for k in klines)
    if not isfinite(low) or not isfinite(high) or high <= low:
        return None
    step = (high - low) / bins
    profile = [0.0] * bins
    for k in klines:
        start = max(0, min(bins - 1, int((k.low - low) / step)))
        end = max(start, min(bins - 1, int((k.high - low) / step)))
        count = end - start + 1
        share = max(0.0, k.volume) / count
        for i in range(start, end + 1):
            profile[i] += share
    total = sum(profile)
    if total <= 0:
        return None
    poc_idx = max(range(bins), key=profile.__getitem__)
    target = total * value_area_pct
    selected = {poc_idx}
    covered = profile[poc_idx]
    left, right = poc_idx - 1, poc_idx + 1
    while covered < target and (left >= 0 or right < bins):
        left_value = profile[left] if left >= 0 else -1.0
        right_value = profile[right] if right < bins else -1.0
        if right_value >= left_value:
            if right < bins:
                selected.add(right)
                covered += right_value
                right += 1
            elif left >= 0:
                selected.add(left)
                covered += left_value
                left -= 1
        else:
            if left >= 0:
                selected.add(left)
                covered += left_value
                left -= 1
            elif right < bins:
                selected.add(right)
                covered += right_value
                right += 1
    va_low, va_high = min(selected), max(selected)
    centers = [low + (i + 0.5) * step for i in range(bins)]
    threshold_hvn = total / bins * 1.5
    threshold_lvn = total / bins * 0.5
    hvn = tuple(centers[i] for i, v in enumerate(profile) if v >= threshold_hvn)
    lvn = tuple(centers[i] for i, v in enumerate(profile) if v <= threshold_lvn)
    price = klines[-1].close
    poc = centers[poc_idx]
    val, vah = centers[va_low], centers[va_high]
    location = _clamp((price - val) / (vah - val)) if vah > val else 0.5
    acceptance = _clamp(profile[poc_idx] / (mean(profile) or 1.0) / 2.0)
    return VolumeProfileContext(
        poc, vah, val, hvn, lvn, round(location, 4), round(acceptance, 4)
    )


def volatility_context(
    klines: Sequence[Kline], period: int = 14, lookback: int = 80
) -> VolatilityContext:
    if len(klines) < 2:
        return VolatilityContext(0.0, 50.0, "normal", 0.0)
    ranges = [
        max(
            k.high - k.low,
            abs(k.high - klines[i - 1].close),
            abs(k.low - klines[i - 1].close),
        )
        for i, k in enumerate(klines[1:], 1)
    ]
    current_window = ranges[-period:] if len(ranges) >= period else ranges
    atr = mean(current_window) if current_window else 0.0
    history: list[float] = []
    for end in range(period, len(ranges) + 1):
        history.append(mean(ranges[end - period : end]))
    history = history[-lookback:]
    percentile = (
        50.0
        if not history
        else 100.0
        * (sum(x < atr for x in history) + 0.5 * sum(x == atr for x in history))
        / len(history)
    )
    previous = mean(ranges[-2 * period : -period]) if len(ranges) >= 2 * period else atr
    expansion_rate = (atr - previous) / previous if previous > 0 else 0.0
    if percentile >= 90:
        regime = "extreme"
    elif percentile >= 65 or expansion_rate >= 0.25:
        regime = "expansion"
    elif percentile <= 20 and expansion_rate <= 0:
        regime = "compression"
    else:
        regime = "normal"
    return VolatilityContext(
        round(atr, 8), round(percentile, 2), regime, round(expansion_rate, 4)
    )


def price_imbalance(klines: Sequence[Kline], max_zones: int = 8) -> ImbalanceContext:
    """Detect three-candle fair-value-gap style price imbalances.

    The result is deliberately named *price imbalance*: it does not assert
    that an institution created the gap.  Zones are ranked by normalized
    displacement magnitude.
    """
    zones: list[tuple[float, float, float]] = []
    if len(klines) >= 3:
        for a, b, c in zip(klines[:-2], klines[1:-1], klines[2:]):
            if c.low > a.high:
                gap = c.low - a.high
                disp = gap / max(b.high - b.low, 1e-12)
                zones.append((a.high, c.low, disp))
            elif c.high < a.low:
                gap = a.low - c.high
                disp = gap / max(b.high - b.low, 1e-12)
                zones.append((c.high, a.low, disp))
    zones.sort(key=lambda z: z[2], reverse=True)
    compact = tuple((round(lo, 8), round(hi, 8)) for lo, hi, _ in zones[:max_zones])
    price = klines[-1].close if klines else 0.0
    above = min((lo for lo, hi in compact if lo > price), default=None)
    below = max((hi for lo, hi in compact if hi < price), default=None)
    displacement = _clamp(
        mean(z[2] / 3.0 for z in zones[:max_zones]) if zones else 0.0
    )
    return ImbalanceContext(compact, above, below, round(displacement, 4))


def structural_symmetry(
    klines: Sequence[Kline], lookback: int = 80, leg_bars: int = 12, top_k: int = 3
) -> SymmetryMatch | None:
    """Find normalized historical left/right swing analogues.

    Current and historical legs are normalized by absolute price displacement
    and time.  This is a probabilistic contextual feature, not a deterministic
    reversal/target rule.  ``failure_distance`` measures how far the current
    path has departed from the best analogue.
    """
    if len(klines) < max(leg_bars * 3, 30):
        return None
    current = klines[-leg_bars:]
    start_price = current[0].close
    end_price = current[-1].close
    current_move = end_price - start_price
    if abs(current_move) <= 1e-12:
        return None
    current_path = [(k.close - start_price) / abs(current_move) for k in current]
    candidates: list[tuple[float, int, float, float]] = []
    max_start = min(len(klines) - leg_bars - 1, lookback)
    for start in range(max_start):
        hist = klines[start : start + leg_bars]
        hmove = hist[-1].close - hist[0].close
        if abs(hmove) <= 1e-12 or (hmove > 0) == (current_move > 0):
            continue
        hist_path = [(k.close - hist[0].close) / abs(hmove) for k in hist]
        rmse = (mean((a - b) ** 2 for a, b in zip(current_path, hist_path))) ** 0.5
        similarity = _clamp(1.0 - rmse / 1.5)
        time_scale = 1.0
        scale = abs(current_move / hmove)
        candidates.append((similarity, start, scale, time_scale))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    best = candidates[:top_k]
    similarity, start, scale, time_scale = best[0]
    hist = klines[start : start + leg_bars]
    projection = []
    for k in hist[-min(4, leg_bars) :]:
        mirrored_delta = (hist[-1].close - k.close) * scale
        projection.append(end_price + mirrored_delta)
    predicted_span = abs(end_price - projection[-1]) if projection else 0.0
    failure_distance = 0.0
    if len(current) >= 3:
        expected_step = current_move / max(leg_bars - 1, 1)
        observed_step = (current[-1].close - current[-3].close) / 2.0
        failure_distance = abs(observed_step - expected_step) / max(
            abs(expected_step), 1e-12
        )
    return SymmetryMatch(
        round(similarity, 4),
        round(scale, 6),
        round(time_scale, 4),
        tuple(round(x, 8) for x in projection),
        True,
        round(min(failure_distance, 10.0), 4),
    )


def forced_flow_proxy(
    klines: Sequence[Kline],
    current_cvd_score: float = 5.0,
    oi_change: float | None = None,
) -> float:
    """Return 0..10 pressure proxy for unusually forced directional flow.

    It intentionally combines price displacement, candle volume anomaly and
    CVD/positioning agreement; exchange liquidation feeds can replace this
    proxy when available without changing the AI contract.
    """
    if len(klines) < 20:
        return 5.0
    volumes = [k.volume for k in klines[-40:-1]]
    baseline = mean(volumes) if volumes else 0.0
    dispersion = pstdev(volumes) if len(volumes) > 1 else 0.0
    z = (klines[-1].volume - baseline) / dispersion if dispersion > 0 else 0.0
    displacement = abs(klines[-1].close - klines[-1].open) / max(
        klines[-1].high - klines[-1].low, 1e-12
    )
    flow = abs(current_cvd_score - 5.0) / 5.0
    positioning = min(1.0, abs(oi_change) * 10.0) if oi_change is not None else 0.5
    return round(
        10.0
        * _clamp(
            (max(0.0, z) / 4.0) * 0.4 + displacement * 0.3 + flow * positioning * 0.3
        ),
        4,
    )


def build_advanced_context(
    klines: Sequence[Kline],
    *,
    current_cvd_score: float = 5.0,
    oi_change: float | None = None,
) -> AdvancedMarketContext:
    vp = volume_profile(klines)
    vol = volatility_context(klines)
    imbalance = price_imbalance(klines)
    symmetry = structural_symmetry(klines)
    forced = forced_flow_proxy(klines, current_cvd_score, oi_change)
    features = {
        "volume_profile_location": vp.price_location if vp else 0.5,
        "volume_profile_acceptance": vp.acceptance_score if vp else 0.0,
        "volatility_percentile": vol.percentile / 100.0,
        "volatility_expansion_rate": vol.expansion_rate,
        "imbalance_displacement": imbalance.displacement_score,
        "forced_flow_pressure": forced / 10.0,
        "symmetry_similarity": symmetry.similarity if symmetry else 0.0,
        "symmetry_failure": symmetry.failure_distance if symmetry else 0.0,
    }
    return AdvancedMarketContext(vp, vol, imbalance, symmetry, forced, features)
