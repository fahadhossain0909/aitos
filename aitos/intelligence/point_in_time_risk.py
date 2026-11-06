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

        correlations: dict[tuple[str, str], float] = {}
        symbols = sorted(prices)
        for index, left in enumerate(symbols):
            for right in symbols[index + 1 :]:
                # Both assets must have prices at both ends of the same
                # interval; never compare a multi-day return with a one-day
                # return merely because the ending timestamps happen to match.
                common = sorted(set(prices[left]).intersection(prices[right]))
                if len(common) - 1 < self.min_observations:
                    continue
                x = []
                y = []
                for previous, current in zip(common, common[1:]):
                    left_previous = prices[left][previous]
                    right_previous = prices[right][previous]
                    if left_previous <= 0 or right_previous <= 0:
                        continue
                    x.append(prices[left][current] / left_previous - 1.0)
                    y.append(prices[right][current] / right_previous - 1.0)
                if len(x) < self.min_observations:
                    continue
                mean_x, mean_y = sum(x) / len(x), sum(y) / len(y)
                centered_x = [value - mean_x for value in x]
                centered_y = [value - mean_y for value in y]
                variance_x = sum(value * value for value in centered_x)
                variance_y = sum(value * value for value in centered_y)
                denominator = (variance_x * variance_y) ** 0.5
                if denominator <= 1e-18:
                    continue
                raw = sum(a * b for a, b in zip(centered_x, centered_y)) / denominator
                confidence = min(1.0, len(x) / self.shrinkage_observations)
                value = max(-1.0, min(1.0, raw * confidence))
                correlations[(left, right)] = round(value, 6)
                correlations[(right, left)] = round(value, 6)
        return correlations

    def estimate_covariance(
        self, observations: Iterable[dict[str, Any]], *, as_of: datetime
    ) -> dict[tuple[str, str], float]:
        """Return pairwise sample covariance of aligned point-in-time returns."""
        cutoff = _timestamp(as_of)
        start = cutoff - self.lookback
        prices: dict[str, dict[datetime, float]] = defaultdict(dict)
        for row in observations:
            symbol = str(row.get("symbol", "")).upper()
            if not symbol:
                continue
            timestamp = _timestamp(row["timestamp"])
            if timestamp < start or timestamp > cutoff:
                continue
            price = float(row["price"])
            if isfinite(price) and price > 0:
                prices[symbol][timestamp] = price

        covariance: dict[tuple[str, str], float] = {}
        symbols = sorted(prices)
        for index, left in enumerate(symbols):
            for right in symbols[index:]:
                common = sorted(set(prices[left]).intersection(prices[right]))
                if len(common) - 1 < self.min_observations:
                    continue
                x = []
                y = []
                for previous, current in zip(common, common[1:]):
                    left_previous = prices[left][previous]
                    right_previous = prices[right][previous]
                    x.append(prices[left][current] / left_previous - 1.0)
                    y.append(prices[right][current] / right_previous - 1.0)
                if len(x) < self.min_observations:
                    continue
                mean_x, mean_y = sum(x) / len(x), sum(y) / len(y)
                value = sum(
                    (a - mean_x) * (b - mean_y) for a, b in zip(x, y)
                ) / (len(x) - 1)
                covariance[(left, right)] = value
                covariance[(right, left)] = value
        return covariance
