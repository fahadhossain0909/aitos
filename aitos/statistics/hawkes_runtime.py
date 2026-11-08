"""Opt-in Hawkes bootstrap from recent exchange trades with OOS acceptance.

Models are installed only when a stationary MLE converges and its held-out
marked-event log likelihood beats a training-rate Poisson baseline. The OOS
window is then replayed into the live filter so the first live observation
starts from a causally warmed state.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from statistics import median
from typing import Iterable

from aitos.models.market import TradeSide
from aitos.statistics.engine import AStatEngine
from aitos.statistics.hawkes import HawkesMLEFit, MarkedHawkesOnlineFilter, fit_marked_hawkes_mle


@dataclass(frozen=True)
class HawkesBootstrapResult:
    symbol: str
    status: str
    events: int
    training_events: int
    holdout_events: int
    oos_gain_per_event: float | None
    branching_ratio: float | None
    reason: str | None = None


def _strict_event_times(trades) -> tuple[list[float], list[int]]:
    ordered = sorted(
        trades,
        key=lambda trade: (trade.timestamp.timestamp(), int(trade.trade_id)),
    )
    if not ordered:
        return [], []
    origin = ordered[0].timestamp.timestamp()
    times: list[float] = []
    marks: list[int] = []
    previous = -math.inf
    for trade in ordered:
        current = trade.timestamp.timestamp() - origin
        if current <= previous:
            current = math.nextafter(previous, math.inf)
        times.append(current)
        marks.append(1 if trade.side == TradeSide.BUY else 0)
        previous = current
    return times, marks


def _holdout_gain(
    fit: HawkesMLEFit,
    train_times: list[float],
    train_marks: list[int],
    holdout_times: list[float],
    holdout_marks: list[int],
    boundary: float,
    end: float,
) -> float:
    """Compare held-out Hawkes log likelihood with a training-rate Poisson null."""
    online = MarkedHawkesOnlineFilter(fit)
    for timestamp, mark in zip(train_times, train_marks):
        online.observe(timestamp, mark)

    hawkes_log_events = 0.0
    for timestamp, mark in zip(holdout_times, holdout_marks):
        snapshot = online.observe(timestamp, mark)
        hawkes_log_events += math.log(max(snapshot.intensities[mark], 1e-12))

    duration = end - boundary
    if duration <= 0:
        raise ValueError("Hawkes holdout duration must be positive")
    training_duration = boundary
    if training_duration <= 0:
        raise ValueError("Hawkes training duration must be positive")
    counts = [train_marks.count(mark) for mark in (0, 1)]
    baseline = [max(count / training_duration, 1e-12) for count in counts]
    poisson_log_events = sum(
        math.log(baseline[mark]) for mark in holdout_marks
    )

    excitation_integral = 0.0
    all_times = train_times + holdout_times
    all_marks = train_marks + holdout_marks
    for source in (0, 1):
        source_tail = sum(
            1.0 - math.exp(-fit.decay * (end - timestamp))
            for timestamp, mark in zip(all_times, all_marks)
            if mark == source and timestamp < end
        )
        excitation_integral += sum(
            fit.excitation_matrix[target][source] * source_tail
            for target in (0, 1)
        )
    hawkes_integral = sum(fit.baseline_intensities) * duration + excitation_integral
    poisson_integral = sum(baseline) * duration
    hawkes_ll = hawkes_log_events - hawkes_integral
    poisson_ll = poisson_log_events - poisson_integral
    return (hawkes_ll - poisson_ll) / max(1, len(holdout_marks))


def _fit_and_accept(
    symbol: str,
    trades,
    *,
    min_events: int,
    holdout_fraction: float,
    min_mark_events: int,
    min_oos_gain_per_event: float,
    decay_grid: tuple[float, ...],
    max_iterations: int,
) -> tuple[HawkesMLEFit | None, list[tuple[float, int]], HawkesBootstrapResult]:
    times, marks = _strict_event_times(trades)
    count = len(times)
    split = int(count * (1.0 - holdout_fraction))
    if count < min_events or split < 20 or count - split < 20:
        result = HawkesBootstrapResult(
            symbol, "skipped", count, split, count - split, None, None,
            "insufficient_events",
        )
        return None, [], result

    train_times, train_marks = times[:split], marks[:split]
    holdout_times, holdout_marks = times[split:], marks[split:]
    if min(train_marks.count(0), train_marks.count(1)) < min_mark_events:
        result = HawkesBootstrapResult(
            symbol, "skipped", count, split, count - split, None, None,
            "insufficient_training_events_per_mark",
        )
        return None, [], result
    if min(holdout_marks.count(0), holdout_marks.count(1)) < 2:
        result = HawkesBootstrapResult(
            symbol, "skipped", count, split, count - split, None, None,
            "insufficient_holdout_events_per_mark",
        )
        return None, [], result

    gaps = [
        right - left
        for left, right in zip(times, times[1:])
        if right > left
    ]
    boundary = (train_times[-1] + holdout_times[0]) / 2.0
    end = holdout_times[-1] + median(gaps)
    try:
        fit = fit_marked_hawkes_mle(
            train_times,
            train_marks,
            horizon=boundary,
            decay_grid=decay_grid,
            max_iterations=max_iterations,
        )
        if not fit.converged or fit.branching_ratio >= 0.95:
            raise ValueError("MLE did not converge to a stationary solution")
        gain = _holdout_gain(
            fit,
            train_times,
            train_marks,
            holdout_times,
            holdout_marks,
            boundary,
            end,
        )
        if not math.isfinite(gain) or gain <= min_oos_gain_per_event:
            result = HawkesBootstrapResult(
                symbol, "rejected", count, split, count - split, gain,
                fit.branching_ratio, "oos_did_not_beat_poisson_baseline",
            )
            return None, [], result
    except Exception as exc:
        result = HawkesBootstrapResult(
            symbol, "rejected", count, split, count - split, None, None,
            f"fit_or_oos_validation_failed:{type(exc).__name__}",
        )
        return None, [], result

    # Absolute epoch timestamps preserve chronology when the live WebSocket
    # consumer takes over. Replay is atomic with model installation.
    warmup: list[tuple[float, int]] = []
    previous_timestamp = -math.inf
    for trade in sorted(
        trades,
        key=lambda trade: (trade.timestamp.timestamp(), int(trade.trade_id)),
    ):
        timestamp = trade.timestamp.timestamp()
        if timestamp <= previous_timestamp:
            timestamp = math.nextafter(previous_timestamp, math.inf)
        warmup.append(
            (timestamp, 1 if trade.side == TradeSide.BUY else 0)
        )
        previous_timestamp = timestamp
    result = HawkesBootstrapResult(
        symbol, "accepted", count, split, count - split, gain,
        fit.branching_ratio, None,
    )
    return fit, warmup, result


async def bootstrap_hawkes_models(
    exchange,
    engine: AStatEngine,
    symbols: Iterable[str],
    *,
    sample_limit: int = 1000,
    min_events: int = 200,
    holdout_fraction: float = 0.2,
    min_mark_events: int = 20,
    min_oos_gain_per_event: float = 0.0,
    decay_grid: tuple[float, ...] = (0.05, 0.1, 0.2, 0.5, 1.0),
    max_iterations: int = 120,
) -> tuple[HawkesBootstrapResult, ...]:
    """Fetch, fit, OOS-validate and atomically install per-symbol Hawkes filters."""
    if not 100 <= sample_limit <= 1000:
        raise ValueError("Hawkes sample_limit must be between 100 and 1000")
    if min_events < 40 or min_events > sample_limit:
        raise ValueError("Hawkes min_events must be between 40 and sample_limit")
    if not 0.1 <= holdout_fraction <= 0.4:
        raise ValueError("Hawkes holdout_fraction must be between 0.1 and 0.4")
    normalized = tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()))
    if not normalized:
        raise ValueError("at least one explicit Hawkes symbol is required")

    results: list[HawkesBootstrapResult] = []
    for symbol in normalized:
        try:
            trades = await exchange.fetch_recent_trades(symbol, limit=sample_limit)
        except Exception as exc:
            results.append(
                HawkesBootstrapResult(
                    symbol, "skipped", 0, 0, 0, None, None,
                    f"exchange_history_unavailable:{type(exc).__name__}",
                )
            )
            continue
        fit, warmup, result = await asyncio.to_thread(
            _fit_and_accept,
            symbol,
            trades,
            min_events=min_events,
            holdout_fraction=holdout_fraction,
            min_mark_events=min_mark_events,
            min_oos_gain_per_event=min_oos_gain_per_event,
            decay_grid=decay_grid,
            max_iterations=max_iterations,
        )
        if fit is not None:
            try:
                engine.register_hawkes_model(symbol, fit, warmup_events=warmup)
            except Exception as exc:
                result = HawkesBootstrapResult(
                    symbol, "rejected", result.events, result.training_events,
                    result.holdout_events, result.oos_gain_per_event,
                    result.branching_ratio,
                    f"live_filter_registration_failed:{type(exc).__name__}",
                )
        results.append(result)
    return tuple(results)
