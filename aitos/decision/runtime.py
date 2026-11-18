"""Fail-closed, advisory-only ADL runtime."""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Mapping

from .aggregation import aggregate_assessments
from .calibration import ProbabilityCalibrator
from .providers import DecisionProvider
from .types import (
    ChoiceQuestion,
    DecisionAction,
    DecisionPolicy,
    DecisionResult,
    DecisionSpec,
    MarketSnapshot,
    ModelResponse,
    NoulQuestion,
    ScoreQuestion,
)

_ACTIONS = tuple(
    action.value for action in DecisionAction if action != DecisionAction.ABSTAIN
)


def _distribution(values: Mapping[str, float]) -> dict[str, float]:
    if set(values) != set(_ACTIONS):
        raise ValueError("provider must return LONG, SHORT and NO_TRADE probabilities")
    result = {}
    for action in _ACTIONS:
        value = values[action]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("probabilities must be numeric")
        if not math.isfinite(value) or value < 0:
            raise ValueError("probabilities must be finite and non-negative")
        result[action] = float(value)
    total = sum(result.values())
    if total <= 0 or abs(total - 1.0) > 1e-6:
        raise ValueError("probabilities must sum to one")
    return result


def _validate_question_outputs(response: ModelResponse, spec: DecisionSpec) -> None:
    """Validate typed DSL outputs before any policy decision is considered."""
    for question in spec.questions:
        if isinstance(question, ChoiceQuestion):
            if set(question.options) == set(_ACTIONS):
                continue
            selected = response.choices.get(question.name)
            if selected not in question.options:
                raise ValueError(f"invalid or missing choice output: {question.name}")
        elif isinstance(question, ScoreQuestion):
            value = response.scores.get(question.name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or not question.minimum <= value <= question.maximum
            ):
                raise ValueError(f"invalid or missing score output: {question.name}")
        elif isinstance(question, NoulQuestion):
            value = response.nouls.get(question.name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                raise ValueError(f"invalid or missing noul output: {question.name}")


class ADLRuntime:
    """Evaluate a decision specification without placing or authorizing orders."""

    def __init__(
        self,
        provider: DecisionProvider,
        *,
        policy: DecisionPolicy | None = None,
        calibrator: ProbabilityCalibrator | None = None,
    ) -> None:
        self.provider = provider
        self.policy = policy or DecisionPolicy()
        self.calibrator = calibrator

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        spec: DecisionSpec,
        *,
        now: datetime | None = None,
    ) -> DecisionResult:
        evaluated_at = now or datetime.now(timezone.utc)
        reasons: list[str] = []
        raw: dict[str, float] = {}
        probabilities: dict[str, float] = {}
        calibrated = False
        disagreement = None
        expected_value = None
        model_id = "unavailable"
        model_version = "unavailable"
        proposed = DecisionAction.ABSTAIN
        choices: dict[str, str] = {}
        scores: dict[str, float] = {}
        nouls: dict[str, float] = {}

        def finish(action: DecisionAction) -> DecisionResult:
            identity = "|".join(
                (
                    snapshot.symbol,
                    snapshot.snapshot_id or snapshot.timestamp.isoformat(),
                    spec.name,
                    evaluated_at.isoformat(),
                    action.value,
                )
            )
            decision_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            return DecisionResult(
                decision_id=decision_id,
                symbol=snapshot.symbol,
                action=action,
                proposed_action=proposed,
                probabilities=probabilities,
                raw_probabilities=raw,
                calibrated=calibrated,
                evidence_disagreement=disagreement,
                expected_value=expected_value,
                reasons=tuple(reasons),
                model_id=model_id,
                model_version=model_version,
                snapshot_id=snapshot.snapshot_id,
                evaluated_at=evaluated_at,
                shadow_mode=self.policy.shadow_mode,
                execution_eligible=False,
                choices=choices,
                scores=scores,
                nouls=nouls,
            )

        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            reasons.append("evaluation clock is not timezone-aware")
            evaluated_at = datetime.now(timezone.utc)
            return finish(DecisionAction.ABSTAIN)
        age = snapshot.age_seconds(evaluated_at)
        if age < 0:
            reasons.append("market snapshot timestamp is in the future")
            return finish(DecisionAction.ABSTAIN)
        if age > self.policy.maximum_snapshot_age_seconds:
            reasons.append("market snapshot is stale")
            return finish(DecisionAction.ABSTAIN)
        missing = [key for key in spec.state_fields if key not in snapshot.features]
        if missing:
            reasons.append("required market state is missing: " + ", ".join(missing))
            return finish(DecisionAction.ABSTAIN)
        for key in self.policy.hard_veto_features:
            if snapshot.features.get(key) is True:
                reasons.append(f"hard risk veto: {key}")
                return finish(DecisionAction.NO_TRADE)

        try:
            response = self.provider.evaluate(snapshot, spec)
            if not isinstance(response, ModelResponse):
                raise ValueError("provider returned an unsupported response type")
            model_id, model_version = response.model_id, response.model_version
            _validate_question_outputs(response, spec)
            choices = dict(response.choices)
            scores = dict(response.scores)
            nouls = dict(response.nouls)
            if response.assessments:
                aggregate = aggregate_assessments(response.assessments)
                raw = _distribution(aggregate.probabilities)
                disagreement = aggregate.disagreement
            else:
                raw = _distribution(response.probabilities)
            probabilities = dict(raw)
            if self.calibrator is not None:
                calibrated_values = {}
                ready = []
                for action, probability in raw.items():
                    value, is_ready = self.calibrator.calibrate(action, probability)
                    calibrated_values[action] = value
                    ready.append(is_ready)
                calibrated = all(ready)
                if calibrated:
                    total = sum(calibrated_values.values())
                    if total <= 0:
                        raise ValueError("calibrated distribution has no positive mass")
                    probabilities = {
                        action: value / total
                        for action, value in calibrated_values.items()
                    }
            if response.economics is not None:
                expected_value = response.economics.expected_value
            elif self.policy.require_positive_expected_value:
                reasons.append("trade economics unavailable")
            proposed = DecisionAction(max(probabilities, key=probabilities.get))
            reasons.extend(response.rationale)
        except Exception as exc:
            reasons.append(f"decision provider/evaluation failed: {type(exc).__name__}")
            return finish(DecisionAction.ABSTAIN)

        if self.policy.require_calibration and not calibrated:
            reasons.append("probability calibration is not mature for every action")
            return finish(DecisionAction.ABSTAIN)
        if disagreement is not None and disagreement > self.policy.maximum_disagreement:
            reasons.append("independent assessments materially disagree")
            return finish(DecisionAction.NO_TRADE)
        if proposed == DecisionAction.NO_TRADE:
            reasons.append("NO_TRADE has the highest assessed probability")
            return finish(DecisionAction.NO_TRADE)
        ordered = sorted(probabilities.values(), reverse=True)
        if ordered[0] < self.policy.minimum_probability:
            reasons.append("top action probability is below policy threshold")
            return finish(DecisionAction.NO_TRADE)
        if ordered[0] - ordered[1] < self.policy.minimum_margin:
            reasons.append("decision probability margin is too narrow")
            return finish(DecisionAction.NO_TRADE)
        if self.policy.require_positive_expected_value:
            if expected_value is None:
                return finish(DecisionAction.ABSTAIN)
            if expected_value <= 0:
                reasons.append("expected value is not positive after costs")
                return finish(DecisionAction.NO_TRADE)
        reasons.append("probabilistic candidate passed ADL advisory policy")
        if self.policy.shadow_mode:
            reasons.append("shadow mode: result cannot authorize execution")
        return finish(proposed)
