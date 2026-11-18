"""Provider adapters; vendor transport is injected and never assumed."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from .types import (
    ChoiceQuestion,
    DecisionSpec,
    MarketSnapshot,
    ModelResponse,
    NoulQuestion,
    ScoreQuestion,
    TradeEconomics,
)


class DecisionProvider(Protocol):
    def evaluate(self, snapshot: MarketSnapshot, spec: DecisionSpec) -> ModelResponse:
        """Return a validated probabilistic assessment."""


class JevDecisionProvider:
    """Thin Jev adapter around an operator-supplied SDK/API callable.

    The actual vendor API contract and credentials must be verified against the
    operator's account. This adapter deliberately invents no endpoint and makes
    no implicit network requests.
    """

    def __init__(
        self,
        invoke: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        *,
        model_id: str = "jev",
        model_version: str = "operator-configured",
    ) -> None:
        if not callable(invoke):
            raise ValueError("Jev invoke callable is required")
        self._invoke = invoke
        self.model_id = model_id
        self.model_version = model_version

    def evaluate(self, snapshot: MarketSnapshot, spec: DecisionSpec) -> ModelResponse:
        payload = {
            "decision_spec": spec.name,
            "state_fields": list(spec.state_fields),
            "questions": [
                {
                    "name": question.name,
                    "kind": question.__class__.__name__,
                    "definition": question.__dict__,
                }
                for question in spec.questions
            ],
            "market_snapshot": {
                "symbol": snapshot.symbol,
                "timestamp": snapshot.timestamp.isoformat(),
                "snapshot_id": snapshot.snapshot_id,
                "features": dict(snapshot.features),
                "source_versions": dict(snapshot.source_versions),
            },
        }
        result = self._invoke(payload)
        if not isinstance(result, Mapping):
            raise ValueError("provider response must be a mapping")
        return ModelResponse(
            probabilities=result.get("probabilities", {}),
            scores=result.get("scores", {}),
            nouls=result.get("nouls", {}),
            choices=result.get("choices", {}),
            economics=(
                TradeEconomics(**result["economics"])
                if isinstance(result.get("economics"), Mapping)
                else None
            ),
            latency_ms=result.get("latency_ms"),
            model_id=str(result.get("model_id", self.model_id)),
            model_version=str(result.get("model_version", self.model_version)),
            rationale=tuple(result.get("rationale", ())),
        )


class NativeEvidenceProvider:
    """Transparent scanner-evidence baseline, not a trained/calibrated model.

    It converts directional evidence into a conservative prior distribution.
    Production policy must keep calibration enabled until matured outcomes
    establish empirical reliability. It intentionally supplies no trade EV.
    """

    def __init__(self, *, maximum_directional_mass: float = 0.80) -> None:
        if not 0.0 < maximum_directional_mass <= 0.95:
            raise ValueError("maximum directional mass must be in (0, 0.95]")
        self.maximum_directional_mass = maximum_directional_mass

    def evaluate(self, snapshot: MarketSnapshot, spec: DecisionSpec) -> ModelResponse:
        direction = str(snapshot.features.get("direction", "")).upper()
        if direction not in {"LONG", "SHORT"}:
            raise ValueError(
                "native evidence provider requires LONG or SHORT direction"
            )
        score = snapshot.features.get("composite_score")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            raise ValueError("scanner composite score must be numeric")
        if not 0.0 <= float(score) <= 100.0:
            raise ValueError("scanner composite score must be between zero and 100")
        directional_mass = min(
            self.maximum_directional_mass,
            max(0.0, (float(score) - 50.0) / 50.0 * self.maximum_directional_mass),
        )
        opposite_mass = directional_mass * 0.25
        selected_mass = directional_mass - opposite_mass
        no_trade_mass = 1.0 - directional_mass
        if direction == "LONG":
            probabilities = {
                "LONG": selected_mass,
                "SHORT": opposite_mass,
                "NO_TRADE": no_trade_mass,
            }
        else:
            probabilities = {
                "LONG": opposite_mass,
                "SHORT": selected_mass,
                "NO_TRADE": no_trade_mass,
            }
        scores = {"scanner_composite_score": float(score)}
        nouls = {}
        choices = {}
        for question in spec.questions:
            if isinstance(question, ScoreQuestion):
                supplied = snapshot.features.get(f"score:{question.name}")
                value = (
                    supplied
                    if supplied is not None
                    else (
                        question.minimum
                        + (float(score) / 100.0) * (question.maximum - question.minimum)
                    )
                )
                scores[question.name] = min(
                    question.maximum, max(question.minimum, float(value))
                )
            elif isinstance(question, NoulQuestion):
                supplied = snapshot.features.get(f"noul:{question.name}")
                if supplied is None and question.name == "reversal":
                    supplied = snapshot.features.get("reversal_probability", 0.5)
                if supplied is not None:
                    nouls[question.name] = float(supplied)
            elif isinstance(question, ChoiceQuestion):
                if set(question.options) != {"LONG", "SHORT", "NO_TRADE"}:
                    selected = snapshot.features.get(f"choice:{question.name}")
                    if selected in question.options:
                        choices[question.name] = selected
        return ModelResponse(
            probabilities=probabilities,
            scores=scores,
            nouls=nouls,
            choices=choices,
            model_id="aitos-native-evidence",
            model_version="heuristic-prior-v1",
            rationale=(
                "heuristic prior derived from scanner score; not empirically calibrated",
                "calibration and expected-value gates remain mandatory",
            ),
        )
