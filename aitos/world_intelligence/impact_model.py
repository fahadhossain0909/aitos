"""Small, deterministic event-impact model for offline research only.

The model is deliberately dependency-free and cohort-specific. It must be
trained on matured historical labels and evaluated on later, purged observations.
It has no runtime trading or model-promotion integration.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from math import exp, isfinite, log


@dataclass(frozen=True, slots=True)
class EventImpactTrainingRow:
    event_id: str
    asset: str
    horizon: str
    regime: str
    event_type: str
    event_time: datetime
    outcome_available_at: datetime
    sentiment: float
    severity: float
    novelty: float
    confidence: float
    source_reliability: float
    age_hours: float
    beta: float
    forward_return: float

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                self.event_id, self.asset, self.horizon, self.regime, self.event_type
            )
        ):
            raise ValueError("event identity, asset, horizon, regime and type are required")
        for name in ("event_time", "outcome_available_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
            object.__setattr__(self, name, value.astimezone(timezone.utc))
        if self.outcome_available_at < self.event_time:
            raise ValueError("outcome cannot be available before event time")
        bounds = {
            "sentiment": (-1.0, 1.0),
            "severity": (0.0, 1.0),
            "novelty": (0.0, 1.0),
            "confidence": (0.0, 1.0),
            "source_reliability": (0.0, 1.0),
            "age_hours": (0.0, 24.0 * 365.0),
            "beta": (-2.0, 2.0),
            "forward_return": (-1.0, 1.0),
        }
        for name, (lower, upper) in bounds.items():
            number = float(getattr(self, name))
            if not isfinite(number) or not lower <= number <= upper:
                raise ValueError(f"{name} must be finite and within [{lower}, {upper}]")
            object.__setattr__(self, name, number)


@dataclass(frozen=True, slots=True)
class EventImpactFeatures:
    asset: str
    horizon: str
    regime: str
    event_type: str
    sentiment: float
    severity: float
    novelty: float
    confidence: float
    source_reliability: float
    age_hours: float
    beta: float

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.asset, self.horizon, self.regime, self.event_type)
        ):
            raise ValueError("asset, horizon, regime and event_type are required")
        bounds = {
            "sentiment": (-1.0, 1.0),
            "severity": (0.0, 1.0),
            "novelty": (0.0, 1.0),
            "confidence": (0.0, 1.0),
            "source_reliability": (0.0, 1.0),
            "age_hours": (0.0, 24.0 * 365.0),
            "beta": (-2.0, 2.0),
        }
        for name, (lower, upper) in bounds.items():
            number = float(getattr(self, name))
            if not isfinite(number) or not lower <= number <= upper:
                raise ValueError(f"{name} must be finite and within [{lower}, {upper}]")
            object.__setattr__(self, name, number)


@dataclass(frozen=True, slots=True)
class EventImpactProbabilityModel:
    asset: str
    horizon: str
    regime: str
    event_types: tuple[str, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float
    training_count: int
    training_as_of: datetime
    regularization: float

    def predict_probability_up(self, features: EventImpactFeatures) -> float:
        if (
            features.asset.upper() != self.asset
            or features.horizon != self.horizon
            or features.regime != self.regime
        ):
            raise ValueError("features do not match the model asset/horizon/regime cohort")
        vector = _raw_features(features, self.event_types)
        standardized = [
            (value - mean) / scale
            for value, mean, scale in zip(vector, self.means, self.scales)
        ]
        score = self.intercept + sum(
            weight * value for weight, value in zip(self.coefficients, standardized)
        )
        score = max(-35.0, min(35.0, score))
        return 1.0 / (1.0 + exp(-score))


def _raw_features(
    row: EventImpactFeatures | EventImpactTrainingRow,
    event_types: tuple[str, ...],
) -> list[float]:
    return [
        row.sentiment,
        row.severity,
        row.novelty,
        row.confidence,
        row.source_reliability,
        min(row.age_hours, 168.0) / 168.0,
        row.beta,
        *(1.0 if row.event_type == name else 0.0 for name in event_types),
    ]


def fit_event_impact_model(
    observations: Sequence[EventImpactTrainingRow],
    *,
    asset: str,
    horizon: str,
    regime: str,
    as_of: datetime,
    min_samples: int = 100,
    epochs: int = 800,
    learning_rate: float = 0.05,
    regularization: float = 0.1,
) -> EventImpactProbabilityModel:
    """Fit a regularized logistic model from labels matured by as_of only."""
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if min_samples < 20 or epochs < 1:
        raise ValueError("min_samples must be >= 20 and epochs must be positive")
    if (
        not isfinite(learning_rate)
        or not 0.0001 <= learning_rate <= 1.0
        or not isfinite(regularization)
        or not 0.0 <= regularization <= 100.0
    ):
        raise ValueError("invalid optimizer policy")
    cutoff = as_of.astimezone(timezone.utc)
    cohort = [
        row
        for row in observations
        if row.asset.upper() == asset.upper()
        and row.horizon == horizon
        and row.regime == regime
        and row.event_time < cutoff
        and row.outcome_available_at <= cutoff
    ]
    if len({row.event_id for row in cohort}) != len(cohort):
        raise ValueError("duplicate event_id in training cohort")
    if len(cohort) < min_samples:
        raise ValueError("insufficient matured observations for model fitting")
    labels = [1.0 if row.forward_return > 0.0 else 0.0 for row in cohort]
    positives = sum(labels)
    if positives == 0 or positives == len(labels):
        raise ValueError("training cohort must contain both positive and negative outcomes")

    event_types = tuple(sorted({row.event_type for row in cohort}))
    matrix = [_raw_features(row, event_types) for row in cohort]
    width = len(matrix[0])
    means = [sum(row[index] for row in matrix) / len(matrix) for index in range(width)]
    scales = []
    for index, mean in enumerate(means):
        variance = sum((row[index] - mean) ** 2 for row in matrix) / len(matrix)
        scales.append(max(variance**0.5, 1e-6))
    matrix = [
        [(value - means[index]) / scales[index] for index, value in enumerate(row)]
        for row in matrix
    ]

    weights = [0.0] * width
    intercept = 0.0
    count = len(matrix)
    for _ in range(epochs):
        gradient = [0.0] * width
        intercept_gradient = 0.0
        for row, label in zip(matrix, labels):
            score = max(-35.0, min(35.0, intercept + sum(w * x for w, x in zip(weights, row))))
            error = 1.0 / (1.0 + exp(-score)) - label
            intercept_gradient += error
            for index, value in enumerate(row):
                gradient[index] += error * value
        intercept -= learning_rate * intercept_gradient / count
        for index in range(width):
            gradient[index] = gradient[index] / count + regularization * weights[index]
            weights[index] -= learning_rate * gradient[index]

    return EventImpactProbabilityModel(
        asset=asset.upper(),
        horizon=horizon,
        regime=regime,
        event_types=event_types,
        means=tuple(means),
        scales=tuple(scales),
        coefficients=tuple(weights),
        intercept=intercept,
        training_count=count,
        training_as_of=cutoff,
        regularization=regularization,
    )


@dataclass(frozen=True, slots=True)
class EventImpactEvaluation:
    sample_count: int
    excluded_pre_training: int
    excluded_unmatured: int
    brier_score: float | None
    log_loss: float | None
    directional_accuracy: float | None
    mean_probability_up: float | None


def evaluate_event_impact_model(
    model: EventImpactProbabilityModel,
    observations: Sequence[EventImpactTrainingRow],
    *,
    as_of: datetime,
) -> EventImpactEvaluation:
    """Evaluate strictly later, matured labels; training observations are purged."""
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    cutoff = as_of.astimezone(timezone.utc)
    if cutoff < model.training_as_of:
        raise ValueError("evaluation cutoff cannot precede model training cutoff")
    cohort = [
        row
        for row in observations
        if row.asset.upper() == model.asset
        and row.horizon == model.horizon
        and row.regime == model.regime
    ]
    if len({row.event_id for row in cohort}) != len(cohort):
        raise ValueError("duplicate event_id in evaluation cohort")
    later = [row for row in cohort if row.event_time > model.training_as_of]
    matured = [row for row in later if row.outcome_available_at <= cutoff]
    if not matured:
        return EventImpactEvaluation(
            0, len(cohort) - len(later), len(later), None, None, None, None
        )
    errors = []
    losses = []
    correct = []
    probabilities = []
    for row in matured:
        features = EventImpactFeatures(
            asset=row.asset,
            horizon=row.horizon,
            regime=row.regime,
            event_type=row.event_type,
            sentiment=row.sentiment,
            severity=row.severity,
            novelty=row.novelty,
            confidence=row.confidence,
            source_reliability=row.source_reliability,
            age_hours=row.age_hours,
            beta=row.beta,
        )
        probability = model.predict_probability_up(features)
        outcome = row.forward_return > 0.0
        label = float(outcome)
        clipped = min(1.0 - 1e-12, max(1e-12, probability))
        errors.append((probability - label) ** 2)
        losses.append(
            -(label * log(clipped) + (1.0 - label) * log(1.0 - clipped))
        )
        correct.append((probability >= 0.5) == outcome)
        probabilities.append(probability)
    count = len(matured)
    return EventImpactEvaluation(
        sample_count=count,
        excluded_pre_training=len(cohort) - len(later),
        excluded_unmatured=len(later) - count,
        brier_score=sum(errors) / count,
        log_loss=sum(losses) / count,
        directional_accuracy=sum(correct) / count,
        mean_probability_up=sum(probabilities) / count,
    )


__all__ = [
    "EventImpactEvaluation",
    "EventImpactFeatures",
    "EventImpactProbabilityModel",
    "EventImpactTrainingRow",
    "evaluate_event_impact_model",
    "fit_event_impact_model",
]
