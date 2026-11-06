"""Point-in-time portfolio correlations from timestamped price history."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from math import isfinite
from typing import Any, Iterable


def _timestamp(value: Any) -> datetime:
    result = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if result.tzinfo is None:
        raise ValueError("price observation timestamps must be timezone-aware")
    return result


class PointInTimeCorrelationEstimator:
    """Pearson correlations using only prices known at the decision cutoff."""

    def __init__(
        self,
        *,
        lookback: timedelta = timedelta(days=30),
        min_observations: int = 20,
        shrinkage_observations: int = 60,
    ) -> None:
        if lookback <= timedelta(0):
            raise ValueError("lookback must be positive")
        if min_observations < 3 or shrinkage_observations < min_observations:
            raise ValueError("invalid correlation sample thresholds")
        self.lookback = lookback
        self.min_observations = min_observations
        self.shrinkage_observations = shrinkage_observations

    def estimate(
        self, observations: Iterable[dict[str, Any]], *, as_of: datetime
    ) -> dict[tuple[str, str], float]:
        cutoff = _timestamp(as_of)
        start = cutoff - self.lookback
        prices: dict[str, dict[datetime, float]] = defaultdict(dict)
        for row in observations:
            symbol = str(row.get("symbol", "")).upper()
            if not symbol:
                continue
            timestamp = _timestamp(row["timestamp"])
            if timestamp > cutoff or timestamp < start:
                continue
            price = float(row["price"])
            if not isfinite(price) or price <= 0:
                continue
            prices[symbol][timestamp] = price

        returns: dict[str, dict[datetime, float]] = {}
        for symbol, series in prices.items():
            ordered = sorted(series.items())
            returns[symbol] = {
                current_time: current_price / previous_price - 1.0
                for (previous_time, previous_price), (
                    current_time,
                    current_price,
                ) in zip(ordered, ordered[1:])
                if current_time > previous_time and previous_price > 0
            }

        correlations: dict[tuple[str, str], float] = {}
        symbols = sorted(returns)
        for index, left in enumerate(symbols):
            for right in symbols[index + 1 :]:
                common = sorted(set(returns[left]).intersection(returns[right]))
                if len(common) < self.min_observations:
                    continue
                x = [returns[left][stamp] for stamp in common]
                y = [returns[right][stamp] for stamp in common]
                mean_x, mean_y = sum(x) / len(x), sum(y) / len(y)
                centered_x = [value - mean_x for value in x]
                centered_y = [value - mean_y for value in y]
                variance_x = sum(value * value for value in centered_x)
                variance_y = sum(value * value for value in centered_y)
                denominator = (variance_x * variance_y) ** 0.5
                if denominator <= 1e-18:
                    continue
                raw = sum(a * b for a, b in zip(centered_x, centered_y)) / denominator
                confidence = min(1.0, len(common) / self.shrinkage_observations)
                value = max(-1.0, min(1.0, raw * confidence))
                correlations[(left, right)] = round(value, 6)
                correlations[(right, left)] = round(value, 6)
        return correlations
