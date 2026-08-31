"""Structural hierarchy helpers for stop selection."""

from __future__ import annotations

TYPE_RANK = {
    "structure_break": 0,
    "protected_swing": 1,
    "major_swing": 2,
    "value_area": 3,
    "liquidity": 4,
    "micro_swing": 5,
    "emergency_fallback": 9,
}

DEFAULT_MICRO_SWING_PCT = 0.003
DEFAULT_HIERARCHY_SLACK_PCT = 0.008


def classify_swing(
    dist_pct: float, micro_pct: float = DEFAULT_MICRO_SWING_PCT
) -> tuple[str, float, int]:
    """Return (type, confidence, rank) for a swing at the given distance fraction."""
    if dist_pct < micro_pct:
        return "micro_swing", 0.45, TYPE_RANK["micro_swing"]
    if dist_pct < micro_pct * 3:
        return "major_swing", 0.70, TYPE_RANK["major_swing"]
    return "protected_swing", 0.80, TYPE_RANK["protected_swing"]


def select_by_hierarchy(
    candidates: list[tuple[float, str, float, int]],
    *,
    side: str,
    entry_price: float,
    hierarchy_slack_pct: float = DEFAULT_HIERARCHY_SLACK_PCT,
) -> tuple[float, str, float] | None:
    """Prefer higher-rank thesis invalidation over micro noise.

    candidates: (price, type, confidence, rank)
    Returns (price, type, confidence) or None.
    """
    if not candidates:
        return None
    valid = list(candidates)
    valid.sort(key=lambda t: (t[3], -t[0] if side == "LONG" else t[0]))
    best_rank = min(c[3] for c in valid)
    best = [c for c in valid if c[3] == best_rank]
    if side == "LONG":
        best.sort(key=lambda t: -t[0])
    else:
        best.sort(key=lambda t: t[0])
    primary = best[0]
    if primary[1] == "micro_swing":
        better = [
            c
            for c in valid
            if c[3] < TYPE_RANK["micro_swing"]
            and abs(c[0] - primary[0]) / entry_price <= hierarchy_slack_pct * 2
        ]
        if better:
            better.sort(key=lambda t: t[3])
            primary = better[0]
    return primary[0], primary[1], primary[2]
