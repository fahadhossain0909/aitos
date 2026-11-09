"""Financial machine-learning primitives for leakage-aware AITOS research.

This module is intentionally side-effect free: it builds event-time samples,
creates forward-looking labels for offline research, and trains a small
meta-label classifier. Labels must never be joined to features before their
label_end timestamp; fit only on purged, chronological training folds.
Nothing here submits orders or overrides primary strategy/risk controls.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from statistics import mean

_EPS = 1e-12


def _finite(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class MarketEvent:
    timestamp: str
    price: float
    volume: float
    signed_volume: float
    event_id: str | None = None

    def __post_init__(self) -> None:
        if not self.timestamp:
            raise ValueError("timestamp is required")
        if self.event_id is not None and not str(self.event_id).strip():
            raise ValueError("event_id cannot be empty")
        if _finite(self.price, "price") <= 0:
            raise ValueError("price must be positive")
        if _finite(self.volume, "volume") < 0:
            raise ValueError("volume must be non-negative")
        signed = _finite(self.signed_volume, "signed_volume")
        if abs(signed) > self.volume + 1e-9:
            raise ValueError("absolute signed_volume cannot exceed volume")


@dataclass(frozen=True)
class EventBar:
    start_timestamp: str
    end_timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    dollar_volume: float
    signed_volume: float
    event_count: int


def _bar(events: Sequence[MarketEvent]) -> EventBar:
    if not events:
        raise ValueError("cannot build an empty event bar")
    prices = [event.price for event in events]
    volume = sum(event.volume for event in events)
    return EventBar(
        start_timestamp=events[0].timestamp,
        end_timestamp=events[-1].timestamp,
        open=events[0].price,
        high=max(prices),
        low=min(prices),
        close=events[-1].price,
        volume=volume,
        dollar_volume=sum(event.price * event.volume for event in events),
        signed_volume=sum(event.signed_volume for event in events),
        event_count=len(events),
    )


def _build_threshold_bars(
    events: Iterable[MarketEvent], threshold: float, measure: str
) -> tuple[EventBar, ...]:
    threshold = _finite(threshold, "threshold")
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    output: list[EventBar] = []
    pending: list[MarketEvent] = []
    accumulated = 0.0
    previous_timestamp = None
    for event in events:
        if previous_timestamp is not None and event.timestamp < previous_timestamp:
            raise ValueError("events must be chronological")
        previous_timestamp = event.timestamp
        pending.append(event)
        if measure == "volume":
            accumulated += event.volume
        elif measure == "dollar":
            accumulated += event.price * event.volume
        else:
            accumulated += event.signed_volume
        crossed = (
            accumulated >= threshold
            if measure != "imbalance"
            else abs(accumulated) >= threshold
        )
        if crossed:
            output.append(_bar(pending))
            pending = []
            accumulated = 0.0
    # An incomplete tail is deliberately omitted; callers may persist it and
    # append future events rather than treating a partial bar as a closed bar.
    return tuple(output)


def build_volume_bars(
    events: Iterable[MarketEvent], *, volume_threshold: float
) -> tuple[EventBar, ...]:
    """Aggregate chronological events until traded volume reaches a threshold."""
    return _build_threshold_bars(events, volume_threshold, "volume")


def build_dollar_bars(
    events: Iterable[MarketEvent], *, dollar_threshold: float
) -> tuple[EventBar, ...]:
    """Aggregate events until price-times-volume reaches a dollar threshold."""
    return _build_threshold_bars(events, dollar_threshold, "dollar")


def build_imbalance_bars(
    events: Iterable[MarketEvent], *, imbalance_threshold: float
) -> tuple[EventBar, ...]:
    """Aggregate events until absolute signed-volume imbalance crosses a limit."""
    return _build_threshold_bars(events, imbalance_threshold, "imbalance")


def fractional_difference_weights(
    order: float, *, threshold: float = 1e-5, max_width: int = 10_000
) -> tuple[float, ...]:
    """Return newest-to-oldest fixed-width fractional-difference weights."""
    order = _finite(order, "order")
    threshold = _finite(threshold, "threshold")
    if not 0.0 < order < 1.0:
        raise ValueError("order must be in (0, 1)")
    if not 0.0 < threshold < 1.0 or max_width < 2:
        raise ValueError("threshold must be in (0,1) and max_width >= 2")
    weights = [1.0]
    for k in range(1, max_width):
        weight = -weights[-1] * (order - k + 1.0) / k
        if abs(weight) < threshold:
            break
        weights.append(weight)
    return tuple(weights)


def fractional_difference(
    values: Iterable[float],
    *,
    order: float = 0.4,
    threshold: float = 1e-5,
    min_periods: int | None = None,
) -> tuple[float | None, ...]:
    """Causally filter a series; output at t uses only x[t], x[t-1], ... .

    Initial observations without a complete weight window are None by default.
    This prevents silently changing the filter's effective memory at the
    beginning of a training or inference window.
    """
    xs = tuple(_finite(value, "values") for value in values)
    weights = fractional_difference_weights(order, threshold=threshold)
    required = min_periods if min_periods is not None else len(weights)
    if required <= 0 or required > len(weights):
        raise ValueError("min_periods must be in [1, weight_count]")
    output: list[float | None] = []
    for index in range(len(xs)):
        width = min(index + 1, len(weights))
        if width < required:
            output.append(None)
            continue
        output.append(
            sum(weights[offset] * xs[index - offset] for offset in range(width))
        )
    return tuple(output)


@dataclass(frozen=True)
class BarrierObservation:
    timestamp: str
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self) -> None:
        if not self.timestamp:
            raise ValueError("timestamp is required")
        values = tuple(
            _finite(getattr(self, name), name)
            for name in ("open", "high", "low", "close")
        )
        opening, high, low, close = values
        if (
            min(values) <= 0
            or high < max(opening, close, low)
            or low > min(opening, close, high)
        ):
            raise ValueError("invalid OHLC bar")


@dataclass(frozen=True)
class TripleBarrierLabel:
    event_index: int
    end_index: int
    start_timestamp: str
    end_timestamp: str
    side: int
    label: int
    gross_return: float
    net_return: float
    barrier: str
    holding_bars: int


def triple_barrier_labels(
    bars: Sequence[BarrierObservation],
    *,
    event_indices: Iterable[int],
    sides: Iterable[int],
    volatility: Sequence[float],
    profit_take: float = 1.0,
    stop_loss: float = 1.0,
    max_holding_bars: int = 20,
    entry_fee_bps: float = 0.0,
    exit_fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
    funding_bps_per_bar: float = 0.0,
) -> tuple[TripleBarrierLabel, ...]:
    """Create side-aware triple-barrier labels from OHLC bars.

    Volatility is a positive decimal return estimate available at each event
    time (not computed from future observations). If both horizontal barriers
    are touched in one OHLC bar, the stop is assumed first (conservative).
    All costs are non-negative bps. A neutral vertical-barrier outcome gets
    label 0; labels are intended for offline training, not live decisions.
    """
    event_indices = tuple(event_indices)
    sides = tuple(sides)
    if len(event_indices) != len(sides):
        raise ValueError("event_indices and sides must align")
    if len(volatility) != len(bars):
        raise ValueError("volatility must align with bars")
    if not bars or max_holding_bars <= 0:
        raise ValueError("bars and positive max_holding_bars are required")
    profit_take = _finite(profit_take, "profit_take")
    stop_loss = _finite(stop_loss, "stop_loss")
    if min(profit_take, stop_loss) <= 0:
        raise ValueError("barrier multiples must be positive")
    costs = tuple(
        _finite(x, "cost")
        for x in (entry_fee_bps, exit_fee_bps, slippage_bps, funding_bps_per_bar)
    )
    if any(value < 0 for value in costs):
        raise ValueError("costs must be non-negative")
    fee_entry, fee_exit, slip, funding = costs
    results = []
    previous_index = -1
    for raw_index, raw_side in zip(event_indices, sides):
        index, side = int(raw_index), int(raw_side)
        if index <= previous_index or index < 0 or index >= len(bars) - 1:
            raise ValueError("event indices must be increasing and have a future bar")
        previous_index = index
        if side not in (-1, 1):
            raise ValueError("side must be +1 (LONG) or -1 (SHORT)")
        vol = _finite(volatility[index], "volatility")
        if vol <= 0:
            raise ValueError("event volatility must be positive")
        entry = bars[index].close
        upper = entry * (1.0 + side * profit_take * vol)
        lower = entry * (1.0 - side * stop_loss * vol)
        upper_price, lower_price = max(upper, lower), min(upper, lower)
        end = min(len(bars) - 1, index + max_holding_bars)
        exit_price = bars[end].close
        barrier = "vertical"
        for cursor in range(index + 1, end + 1):
            bar = bars[cursor]
            # Opening gaps are filled at the observed open, not at an
            # unreachable barrier price. Intrabar double touches remain
            # conservatively stop-first because OHLC has no path ordering.
            stop_gap = bar.open <= lower_price if side == 1 else bar.open >= upper_price
            take_gap = bar.open >= upper_price if side == 1 else bar.open <= lower_price
            if stop_gap:
                exit_price, end, barrier = bar.open, cursor, "stop"
                break
            if take_gap:
                exit_price, end, barrier = bar.open, cursor, "take"
                break
            stop_hit = bar.low <= lower_price if side == 1 else bar.high >= upper_price
            take_hit = bar.high >= upper_price if side == 1 else bar.low <= lower_price
            if stop_hit:
                exit_price, end, barrier = lower_price, cursor, "stop"
                break
            if take_hit:
                exit_price, end, barrier = upper_price, cursor, "take"
                break
        gross = side * (exit_price / entry - 1.0)
        total_cost_bps = fee_entry + fee_exit + slip + funding * (end - index)
        net = gross - total_cost_bps / 10_000.0
        label = 1 if net > 0 else (-1 if net < 0 else 0)
        results.append(
            TripleBarrierLabel(
                event_index=index,
                end_index=end,
                start_timestamp=bars[index].timestamp,
                end_timestamp=bars[end].timestamp,
                side=side,
                label=label,
                gross_return=gross,
                net_return=net,
                barrier=barrier,
                holding_bars=end - index,
            )
        )
    return tuple(results)


@dataclass(frozen=True)
class MetaLabelPrediction:
    probability: float
    accepted: bool
    threshold: float


class MetaLabeler:
    """Small deterministic logistic meta-model; primary side is an input fact.

    Fit on binary labels indicating whether the primary strategy's proposed
    trade achieved a positive net outcome. The model only estimates acceptance
    probability. Callers must still apply existing hard risk/HTF/execution gates.
    Standardization and model coefficients are learned from training data only.
    """

    def __init__(
        self, *, learning_rate: float = 0.05, l2: float = 1e-3, epochs: int = 1000
    ):
        if not math.isfinite(learning_rate) or learning_rate <= 0:
            raise ValueError("learning_rate must be positive")
        if not math.isfinite(l2) or l2 < 0 or epochs <= 0:
            raise ValueError("l2 must be non-negative and epochs positive")
        self.learning_rate = learning_rate
        self.l2 = l2
        self.epochs = epochs
        self.means: tuple[float, ...] = ()
        self.scales: tuple[float, ...] = ()
        self.coefficients: tuple[float, ...] = ()
        self.intercept = 0.0
        self.fitted = False

    @staticmethod
    def _matrix(
        features: Sequence[Sequence[float]], labels: Sequence[int] | None = None
    ):
        rows = tuple(
            tuple(_finite(value, "feature") for value in row) for row in features
        )
        if not rows or not rows[0] or any(len(row) != len(rows[0]) for row in rows):
            raise ValueError("features must be a non-empty rectangular matrix")
        if labels is not None and (
            len(labels) != len(rows) or any(value not in (0, 1) for value in labels)
        ):
            raise ValueError("binary labels must align with features")
        return rows

    @staticmethod
    def _sigmoid(value: float) -> float:
        if value >= 0:
            z = math.exp(-min(value, 700.0))
            return 1.0 / (1.0 + z)
        z = math.exp(max(value, -700.0))
        return z / (1.0 + z)

    def fit(
        self, features: Sequence[Sequence[float]], labels: Sequence[int]
    ) -> MetaLabeler:
        rows = self._matrix(features, labels)
        ys = tuple(int(value) for value in labels)
        if len(set(ys)) != 2:
            raise ValueError("training requires both positive and negative meta-labels")
        width = len(rows[0])
        means = tuple(mean(row[col] for row in rows) for col in range(width))
        scales = tuple(
            max(math.sqrt(mean((row[col] - means[col]) ** 2 for row in rows)), 1e-9)
            for col in range(width)
        )
        standardized = tuple(
            tuple((row[col] - means[col]) / scales[col] for col in range(width))
            for row in rows
        )
        weights = [0.0] * width
        intercept = 0.0
        count = len(rows)
        for _ in range(self.epochs):
            grad = [0.0] * width
            bias_grad = 0.0
            for row, target in zip(standardized, ys):
                probability = self._sigmoid(
                    intercept + sum(w * x for w, x in zip(weights, row))
                )
                error = probability - target
                bias_grad += error
                for col, value in enumerate(row):
                    grad[col] += error * value
            intercept -= self.learning_rate * bias_grad / count
            for col in range(width):
                weights[col] -= self.learning_rate * (
                    grad[col] / count + self.l2 * weights[col]
                )
        self.means, self.scales = means, scales
        self.coefficients, self.intercept = tuple(weights), intercept
        self.fitted = True
        return self

    def predict_probability(
        self, features: Sequence[Sequence[float]]
    ) -> tuple[float, ...]:
        if not self.fitted:
            raise RuntimeError("meta-labeler must be fitted before prediction")
        rows = self._matrix(features)
        if len(rows[0]) != len(self.coefficients):
            raise ValueError("feature width differs from fitted model")
        return tuple(
            self._sigmoid(
                self.intercept
                + sum(
                    weight * ((value - self.means[index]) / self.scales[index])
                    for index, (weight, value) in enumerate(zip(self.coefficients, row))
                )
            )
            for row in rows
        )

    def predict(
        self, features: Sequence[Sequence[float]], *, threshold: float = 0.5
    ) -> tuple[MetaLabelPrediction, ...]:
        threshold = _finite(threshold, "threshold")
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be in [0,1]")
        return tuple(
            MetaLabelPrediction(probability, probability >= threshold, threshold)
            for probability in self.predict_probability(features)
        )


def sequential_bootstrap_indices(
    event_starts: Sequence[int],
    event_ends: Sequence[int],
    *,
    sample_count: int | None = None,
    seed: int = 0,
) -> tuple[int, ...]:
    """Sample event intervals with probability proportional to marginal uniqueness.

    Each interval is inclusive. Sampling is with replacement; after each draw,
    concurrency is updated and the next draw is weighted by mean uniqueness.
    This reduces repeated selection of heavily overlapping labels. It is a
    research resampler, not a replacement for purging/embargo in validation.
    """
    starts, ends = tuple(event_starts), tuple(event_ends)
    if not starts or len(starts) != len(ends):
        raise ValueError("event starts and ends must be non-empty and aligned")
    if any(int(start) < 0 or int(end) < int(start) for start, end in zip(starts, ends)):
        raise ValueError("event intervals must be non-negative and start <= end")
    count = sample_count if sample_count is not None else len(starts)
    if count <= 0:
        raise ValueError("sample_count must be positive")
    intervals = tuple((int(start), int(end)) for start, end in zip(starts, ends))
    concurrency: dict[int, int] = {}
    rng = random.Random(seed)
    selected = []
    for _ in range(count):
        uniqueness = []
        for start, end in intervals:
            score = mean(
                1.0 / (concurrency.get(tick, 0) + 1) for tick in range(start, end + 1)
            )
            uniqueness.append(score)
        total = sum(uniqueness)
        if total <= _EPS:
            weights = [1.0 / len(intervals)] * len(intervals)
        else:
            weights = [value / total for value in uniqueness]
        draw = rng.random()
        cumulative = 0.0
        chosen = len(intervals) - 1
        for index, probability in enumerate(weights):
            cumulative += probability
            if draw <= cumulative:
                chosen = index
                break
        selected.append(chosen)
        start, end = intervals[chosen]
        for tick in range(start, end + 1):
            concurrency[tick] = concurrency.get(tick, 0) + 1
    return tuple(selected)
