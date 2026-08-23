"""Advanced Market Profile features built from volume-at-price data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .volume_profile import VolumeProfile


@dataclass(frozen=True)
class ProfileFeatures:
    developing_poc: float
    naked_poc: tuple[float, ...]
    hvn: tuple[float, ...]
    lvn: tuple[float, ...]
    excess_high: float | None
    excess_low: float | None
    poor_high: bool
    poor_low: bool
    single_prints: tuple[float, ...]
    distribution_peaks: tuple[float, ...]


def _local_peaks(
    bins: Sequence[tuple[float, float]], multiplier: float = 1.5
) -> list[int]:
    if len(bins) < 3:
        return []
    mean = sum(v for _, v in bins) / len(bins)
    return [
        i
        for i in range(1, len(bins) - 1)
        if bins[i][1] >= mean * multiplier
        and bins[i][1] >= bins[i - 1][1]
        and bins[i][1] >= bins[i + 1][1]
    ]


def compute_profile_features(
    profile: VolumeProfile, previous_profiles: Sequence[VolumeProfile] = ()
) -> ProfileFeatures:
    if not profile.bins:
        return ProfileFeatures(0.0, (), (), (), None, None, False, False, (), ())
    bins = profile.bins
    volumes = [v for _, v in bins]
    mean = sum(volumes) / len(volumes)
    hvn = tuple(p for p, v in bins if v >= mean * 1.25)
    lvn = tuple(p for p, v in bins if v <= mean * 0.50)
    peaks = _local_peaks(bins)
    developing_poc = profile.poc

    naked: list[float] = []
    for old in previous_profiles:
        touched = any(p == old.poc and v > 0 for p, v in bins)
        if not touched:
            naked.append(old.poc)

    edge = max(1, min(3, len(bins) // 10))
    excess_high = bins[-1][0] if bins[-1][1] <= mean * 0.25 else None
    excess_low = bins[0][0] if bins[0][1] <= mean * 0.25 else None
    poor_high = sum(v > 0 for _, v in bins[-edge:]) <= 1
    poor_low = sum(v > 0 for _, v in bins[:edge]) <= 1
    single_prints = tuple(p for p, v in bins if 0 < v <= mean * 0.20)
    return ProfileFeatures(
        developing_poc=developing_poc,
        naked_poc=tuple(dict.fromkeys(naked)),
        hvn=hvn,
        lvn=lvn,
        excess_high=excess_high,
        excess_low=excess_low,
        poor_high=poor_high,
        poor_low=poor_low,
        single_prints=single_prints,
        distribution_peaks=tuple(bins[i][0] for i in peaks),
    )
