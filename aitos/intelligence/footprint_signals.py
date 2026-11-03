"""Conservative signals derived from executed-trade footprints."""

from __future__ import annotations

from dataclasses import dataclass

from aitos.intelligence.footprint import Footprint


@dataclass(frozen=True)
class FootprintSignals:
    delta_score: float
    imbalance_score: float
    absorption_score: float
    exhaustion_score: float
    bias: str


class FootprintSignalEngine:
    def __init__(
        self, imbalance_threshold: float = 0.60, min_level_volume: float = 0.0
    ) -> None:
        self.imbalance_threshold = max(0.05, min(0.99, imbalance_threshold))
        self.min_level_volume = max(0.0, min_level_volume)

    def evaluate(self, footprint: Footprint | None) -> FootprintSignals:
        if footprint is None or not footprint.levels:
            return FootprintSignals(5.0, 5.0, 0.0, 0.0, "neutral")

        total = footprint.total_volume
        delta = footprint.total_delta
        delta_ratio = delta / total if total else 0.0
        delta_score = max(0.0, min(10.0, 5.0 + delta_ratio * 5.0))

        eligible = [
            l for l in footprint.levels if l.total_volume >= self.min_level_volume
        ]
        strong = [l for l in eligible if abs(l.imbalance) >= self.imbalance_threshold]
        if strong:
            signed = sum(l.imbalance * l.total_volume for l in strong)
            denom = sum(l.total_volume for l in strong)
            imbalance_ratio = signed / denom if denom else 0.0
        else:
            imbalance_ratio = 0.0
        imbalance_score = max(0.0, min(10.0, 5.0 + imbalance_ratio * 5.0))

        # Absorption proxy: high volume with comparatively small net delta.
        gross = sum(l.total_volume for l in eligible)
        abs_delta = sum(abs(l.delta) for l in eligible)
        absorption_ratio = gross / max(abs_delta, 1e-12) if gross else 0.0
        absorption_score = (
            min(10.0, max(0.0, (absorption_ratio - 1.0) * 2.0)) if gross else 0.0
        )

        # Exhaustion proxy: dominant delta concentrated in a low-volume tail.
        max_level = footprint.max_delta_level
        concentration = abs(max_level.delta) / total if max_level and total else 0.0
        exhaustion_score = (
            min(10.0, concentration * 20.0)
            if max_level
            and max_level.total_volume <= max(total * 0.15, self.min_level_volume)
            else 0.0
        )

        if delta_score >= 6.0 and imbalance_score >= 6.0:
            bias = "bullish"
        elif delta_score <= 4.0 and imbalance_score <= 4.0:
            bias = "bearish"
        else:
            bias = "neutral"
        return FootprintSignals(
            delta_score, imbalance_score, absorption_score, exhaustion_score, bias
        )
