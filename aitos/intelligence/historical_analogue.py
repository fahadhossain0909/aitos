"""Historical analogue search and temporal state-transition context.

The engine is intentionally deterministic and model-agnostic.  It compares
normalized OHLC paths and lightweight market-state features, then reports the
empirical distribution of forward returns for the matched historical states.
It is a contextual evidence source, never a standalone trade signal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import mean

from aitos.models.market import Kline


@dataclass(frozen=True)
class AnalogueOutcome:
    up_probability: float
    down_probability: float
    flat_probability: float
    median_return: float
    best_return: float
    worst_return: float

    def to_dict(self) -> dict[str, float]:
        return {
            "up_probability": round(self.up_probability, 4),
            "down_probability": round(self.down_probability, 4),
            "flat_probability": round(self.flat_probability, 4),
            "median_return": round(self.median_return, 6),
            "best_return": round(self.best_return, 6),
            "worst_return": round(self.worst_return, 6),
        }


@dataclass(frozen=True)
class HistoricalAnalogue:
    start_index: int
    similarity: float
    direction: str
    scale: float
    outcome: AnalogueOutcome | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "start_index": self.start_index,
            "similarity": round(self.similarity, 4),
            "direction": self.direction,
            "scale": round(self.scale, 6),
            "outcome": self.outcome.to_dict() if self.outcome else None,
        }


@dataclass(frozen=True)
class StateTransition:
    previous_state: str
    current_state: str
    transition_score: float
    persistence: float
    reversal_pressure: float

    def to_dict(self) -> dict[str, object]:
        return {
            "previous_state": self.previous_state,
            "current_state": self.current_state,
            "transition_score": round(self.transition_score, 4),
            "persistence": round(self.persistence, 4),
            "reversal_pressure": round(self.reversal_pressure, 4),
        }


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _path(klines: Sequence[Kline]) -> list[float]:
    if not klines:
        return []
    base = klines[0].close
    span = max(max(k.high for k in klines) - min(k.low for k in klines), 1e-12)
    return [(k.close - base) / span for k in klines]


def _return_distribution(
    klines: Sequence[Kline], starts: Sequence[int], horizon: int
) -> AnalogueOutcome | None:
    returns: list[float] = []
    for start in starts:
        end = start + horizon
        if end >= len(klines):
            continue
        base = klines[start].close
        if base:
            returns.append((klines[end].close - base) / base)
    if not returns:
        return None
    ordered = sorted(returns)
    median = ordered[len(ordered) // 2]
    flat_band = max(0.001, mean(abs(x) for x in returns) * 0.15)
    up = sum(x > flat_band for x in returns) / len(returns)
    down = sum(x < -flat_band for x in returns) / len(returns)
    flat = max(0.0, 1.0 - up - down)
    return AnalogueOutcome(up, down, flat, median, max(returns), min(returns))


def search_historical_analogues(
    klines: Sequence[Kline],
    *,
    window: int = 20,
    search_back: int = 500,
    top_k: int = 20,
    forward_horizon: int = 12,
) -> tuple[HistoricalAnalogue, ...]:
    """Find the closest prior normalized price-state analogues.

    Candidates are strictly prior to the current window and do not overlap it,
    preventing look-ahead leakage.  The returned outcome distribution is built
    only from prices after each historical match.
    """
    if len(klines) < window * 2 + 1 or window < 5 or top_k < 1:
        return ()
    current = klines[-window:]
    current_path = _path(current)
    current_move = current[-1].close - current[0].close
    if abs(current_move) <= 1e-12:
        return ()
    current_direction = "up" if current_move > 0 else "down"
    candidates: list[tuple[float, int, float]] = []
    last_start = min(len(klines) - window - 1, search_back)
    for start in range(last_start + 1):
        if start + window >= len(klines) - forward_horizon:
            continue
        hist = klines[start : start + window]
        hist_move = hist[-1].close - hist[0].close
        if abs(hist_move) <= 1e-12:
            continue
        hist_path = _path(hist)
        rmse = (mean((a - b) ** 2 for a, b in zip(current_path, hist_path))) ** 0.5
        similarity = _clamp(1.0 - rmse / 1.25)
        scale = abs(current_move / hist_move)
        candidates.append((similarity, start, scale))
    candidates.sort(reverse=True)
    selected = candidates[:top_k]
    starts = [x[1] for x in selected]
    outcome = _return_distribution(klines, starts, forward_horizon)
    return tuple(
        HistoricalAnalogue(
            start_index=start,
            similarity=similarity,
            direction=(
                "up"
                if klines[start + window - 1].close > klines[start].close
                else "down"
            ),
            scale=scale,
            outcome=outcome,
        )
        for similarity, start, scale in selected
    )


def infer_state_transition(
    previous_state: str, current_state: str, persistence: float = 0.5
) -> StateTransition:
    """Quantify a state change without assuming that transitions predict price."""
    previous = previous_state.lower().strip() or "unknown"
    current = current_state.lower().strip() or "unknown"
    changed = previous != current
    reversal_terms = {
        "reversal",
        "transition",
        "distribution",
        "exhaustion",
        "compression",
    }
    reversal = 1.0 if any(term in current for term in reversal_terms) else 0.0
    return StateTransition(
        previous_state=previous,
        current_state=current,
        transition_score=1.0 if changed else 0.0,
        persistence=_clamp(float(persistence)),
        reversal_pressure=reversal,
    )
