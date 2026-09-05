"""A-Stat orchestration and calibration layer."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable

from .models import (
    AStatObservation,
    AStatResult,
    BayesianEvidence,
    DirectionProbability,
    RegimeProbability,
)
from .stack import ContractStatisticalStack


class AStatEngine:
    """Market-neutral statistical engine with an advanced per-contract stack."""

    WEIGHTS = {
        "momentum": 1.20,
        "return": 1.00,
        "orderbook_imbalance": 1.10,
        "cvd": 0.90,
        "delta": 0.70,
        "vwap_distance": 0.60,
        "funding": -0.25,
        "open_interest": 0.35,
        "market_breadth": 0.75,
        "lead_lag": 0.65,
    }

    def __init__(
        self, calibration_window: int = 256, calibration_quality: float = 0.5
    ) -> None:
        if calibration_window <= 0:
            raise ValueError("calibration_window must be positive")
        if not 0.0 <= float(calibration_quality) <= 1.0:
            raise ValueError("calibration_quality must be between 0 and 1")
        self._calibration_quality = float(calibration_quality)
        self._outcomes: deque[tuple[float, bool]] = deque(maxlen=calibration_window)
        self._returns: deque[float] = deque(maxlen=calibration_window)
        self._stack = ContractStatisticalStack(
            max_history=max(1024, calibration_window * 8)
        )

    def update(self, realised_return: float, predicted_up: float) -> None:
        r = float(realised_return)
        self._returns.append(r)
        self._outcomes.append((max(0.0, min(1.0, float(predicted_up))), r > 0.0))

    def _score(self, features: dict[str, float]) -> float:
        return sum(
            self.WEIGHTS.get(k, 0.0) * float(v)
            for k, v in features.items()
            if k != "returns"
        )

    @staticmethod
    def bayesian_update(prior: float, evidence: Iterable[BayesianEvidence]) -> float:
        p = max(1e-6, min(1.0 - 1e-6, float(prior)))
        odds = p / (1.0 - p)
        for item in evidence:
            odds *= item.likelihood_ratio
        return odds / (1.0 + odds)

    @staticmethod
    def _regime(score: float, volatility: float) -> RegimeProbability:
        trend = min(1.0, abs(score) / 4.0)
        high = min(1.0, volatility / 0.02)
        return RegimeProbability(
            trend_up=trend if score > 0 else 0.0,
            trend_down=trend if score < 0 else 0.0,
            range=max(0.0, 1.0 - trend),
            high_volatility=high,
            low_volatility=1.0 - high,
        ).normalised()

    def _calibration(self, outcomes: deque[tuple[float, bool]] | None = None) -> float:
        """Return calibration quality, falling back to configured baseline."""
        observations = self._outcomes if outcomes is None else outcomes
        if not observations:
            return self._calibration_quality
        brier = sum((p - float(y)) ** 2 for p, y in observations) / len(observations)
        observed_quality = max(0.0, min(1.0, 1.0 - brier / 0.25))
        # The configured value is the prior quality before online evidence is
        # available. Once observations exist, the empirical calibration is the
        # authoritative signal.
        return observed_quality

    def evaluate(
        self, observation: AStatObservation, evidence: Iterable[BayesianEvidence] = ()
    ) -> AStatResult:
        evidence = tuple(evidence)
        score = self._score(observation.features)
        prior = self.bayesian_update(observation.prior_up, evidence)
        raw_up = 1.0 / (1.0 + math.exp(-max(-12.0, min(12.0, score))))
        up = 0.5 * prior + 0.5 * raw_up
        down = 1.0 - up
        flat = max(0.0, 1.0 - min(1.0, abs(score) / 3.0)) * 0.35
        scale = up + down + flat
        direction = DirectionProbability(up / scale, down / scale, flat / scale)
        returns = observation.features.get("returns", ())
        advanced = (
            self._stack.evaluate(observation.symbol, returns)
            if isinstance(returns, (tuple, list)) and returns
            else None
        )
        if advanced is not None:
            expected_return = advanced.expected_return
            volatility = advanced.garch.volatility
            downside = advanced.downside_probability
            tail = advanced.tail_probability
            confidence = 0.6 * advanced.confidence + 0.4 * self._calibration(
                self._outcomes
            )
            regime = self._regime(score, volatility)
        else:
            volatility = abs(float(observation.features.get("volatility", 0.01)))
            expected_return = float(
                observation.features.get(
                    "expected_return", (direction.up - direction.down) * volatility
                )
            )
            downside = 1.0 - direction.up
            tail = downside * 0.1
            confidence = min(
                1.0,
                0.5 * self._calibration(self._outcomes)
                + 0.5 * math.log1p(observation.sample_size) / math.log(1001.0),
            )
            regime = self._regime(score, volatility)
        expected_value = expected_return - downside * volatility
        return AStatResult(
            symbol=observation.symbol,
            horizon=observation.horizon,
            direction=direction,
            regime=regime,
            expected_return=expected_return,
            expected_volatility=volatility,
            downside_probability=max(0.0, min(1.0, downside)),
            tail_loss_probability=max(0.0, min(1.0, tail)),
            expected_value=expected_value,
            probability_confidence=max(0.0, min(1.0, confidence)),
            calibration_quality=self._calibration(self._outcomes),
            sample_size=max(observation.sample_size, len(self._returns)),
            evidence=tuple(item.name for item in evidence),
            advanced=advanced,
        )
