"""Timestamped contracts for reproducible statistical feature and forecast data."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_datetime(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class FeatureSnapshot:
    """Immutable-by-contract feature observation with explicit source lineage."""

    symbol: str
    horizon: str
    as_of: str
    window_start: str
    values: Mapping[str, float] = field(default_factory=dict)
    source_watermarks: Mapping[str, str] = field(default_factory=dict)
    sample_count: int = 0
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if not self.symbol.strip() or not self.horizon.strip():
            raise ValueError("symbol and horizon are required")
        if self.sample_count < 0:
            raise ValueError("sample_count must be non-negative")
        as_of = _utc_datetime(self.as_of, "as_of")
        window_start = _utc_datetime(self.window_start, "window_start")
        if window_start > as_of:
            raise ValueError("window_start cannot be after as_of")
        normalized: dict[str, float] = {}
        for key, value in self.values.items():
            number = float(value)
            if not key or not math.isfinite(number):
                raise ValueError("feature names and values must be valid and finite")
            normalized[str(key)] = number
        for source, watermark in self.source_watermarks.items():
            if not source:
                raise ValueError("source watermark names must not be empty")
            parsed = _utc_datetime(watermark, f"source_watermarks[{source}]")
            if parsed > as_of:
                raise ValueError("source watermark cannot be after snapshot as_of")
        object.__setattr__(self, "values", normalized)
        object.__setattr__(self, "source_watermarks", dict(self.source_watermarks))

    def age_seconds(self, *, now: datetime | None = None) -> float:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return (
            current.astimezone(timezone.utc) - _utc_datetime(self.as_of, "as_of")
        ).total_seconds()

    def is_fresh(self, max_age_seconds: float, *, now: datetime | None = None) -> bool:
        if not math.isfinite(max_age_seconds) or max_age_seconds < 0:
            raise ValueError("max_age_seconds must be finite and non-negative")
        return 0.0 <= self.age_seconds(now=now) <= max_age_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "horizon": self.horizon,
            "as_of": self.as_of,
            "window_start": self.window_start,
            "sample_count": self.sample_count,
            "values": dict(self.values),
            "source_watermarks": dict(self.source_watermarks),
        }


@dataclass(frozen=True)
class ForecastEnvelope:
    """Forecast metadata required before downstream policy may consume a result."""

    symbol: str
    horizon: str
    model_id: str
    model_version: str
    as_of: str
    feature_snapshot_id: str
    sample_count: int
    expected_return: float
    expected_volatility: float
    downside_probability: float
    tail_probability: float
    expected_shortfall: float | None = None
    side: str = "BOTH"

    def __post_init__(self) -> None:
        if not all(
            (
                self.symbol,
                self.horizon,
                self.model_id,
                self.model_version,
                self.feature_snapshot_id,
            )
        ):
            raise ValueError("forecast identity fields are required")
        if self.side not in {"LONG", "SHORT", "BOTH"}:
            raise ValueError("forecast side must be LONG, SHORT or BOTH")
        _utc_datetime(self.as_of, "as_of")
        if self.sample_count < 0:
            raise ValueError("sample_count must be non-negative")
        for name in (
            "expected_return",
            "expected_volatility",
            "downside_probability",
            "tail_probability",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            if name != "expected_return" and value < 0:
                raise ValueError(f"{name} must be non-negative")
        if not 0.0 <= self.downside_probability <= 1.0:
            raise ValueError("downside_probability must be in [0, 1]")
        if not 0.0 <= self.tail_probability <= 1.0:
            raise ValueError("tail_probability must be in [0, 1]")
        if self.expected_shortfall is not None and (
            not math.isfinite(float(self.expected_shortfall))
            or self.expected_shortfall < 0
        ):
            raise ValueError("expected_shortfall must be finite and non-negative")

    def is_fresh(self, max_age_seconds: float, *, now: datetime | None = None) -> bool:
        if not math.isfinite(max_age_seconds) or max_age_seconds < 0:
            raise ValueError("max_age_seconds must be finite and non-negative")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        age = (
            current.astimezone(timezone.utc) - _utc_datetime(self.as_of, "as_of")
        ).total_seconds()
        return 0.0 <= age <= max_age_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "horizon": self.horizon,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "as_of": self.as_of,
            "feature_snapshot_id": self.feature_snapshot_id,
            "sample_count": self.sample_count,
            "expected_return": self.expected_return,
            "expected_volatility": self.expected_volatility,
            "downside_probability": self.downside_probability,
            "tail_probability": self.tail_probability,
            "expected_shortfall": self.expected_shortfall,
        }
