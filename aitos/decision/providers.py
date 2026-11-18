"""Provider adapters; vendor transport is injected and never assumed."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from .types import DecisionSpec, MarketSnapshot, ModelResponse


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
            model_id=str(result.get("model_id", self.model_id)),
            model_version=str(result.get("model_version", self.model_version)),
            rationale=tuple(result.get("rationale", ())),
        )
