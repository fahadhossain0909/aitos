"""TradeOutcomeClassifier — online, durable trade-outcome classifier."""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from sklearn.linear_model import SGDClassifier

FEATURE_ORDER: List[str] = [
    "trend_strength",
    "liquidity_quality",
    "order_flow_bias",
    "auction_context",
    "volatility",
    "market_regime",
    "lead_lag",
    "funding_rate",
    "open_interest_trend",
    "rl_confidence",
]

DEFAULT_MIN_SAMPLES = 30
BACKGROUND_BUFFER_SIZE = 200


def _vectorize(component_scores: Dict[str, float]) -> np.ndarray:
    return np.array(
        [[float(component_scores.get(f, 5.0)) for f in FEATURE_ORDER]], dtype=float
    )


class TradeOutcomeClassifier:
    """Incrementally trained win/loss classifier with durable state."""

    def __init__(
        self,
        min_samples_for_ready: int = DEFAULT_MIN_SAMPLES,
        state_path: str = "models/online_ml/trade_outcome.pkl",
    ) -> None:
        self._model = SGDClassifier(loss="log_loss", random_state=0)
        self._min_samples = min_samples_for_ready
        self._n_samples_seen = 0
        self._classes_seen: set = set()
        self._background: List[List[float]] = []
        self._state_path = Path(state_path)

    @property
    def n_samples_seen(self) -> int:
        return self._n_samples_seen

    @property
    def is_ready(self) -> bool:
        return self._n_samples_seen >= self._min_samples and self._classes_seen == {
            0,
            1,
        }

    def partial_fit(self, component_scores: Dict[str, float], won: bool) -> None:
        X = _vectorize(component_scores)
        y = np.array([1 if won else 0])
        if self._n_samples_seen == 0:
            self._model.partial_fit(X, y, classes=np.array([0, 1]))
        else:
            self._model.partial_fit(X, y)
        self._n_samples_seen += 1
        self._classes_seen.add(int(y[0]))
        self._background.append(X[0].tolist())
        if len(self._background) > BACKGROUND_BUFFER_SIZE:
            self._background.pop(0)

    def predict_win_probability(
        self, component_scores: Dict[str, float]
    ) -> Optional[float]:
        if not self.is_ready:
            return None
        X = _vectorize(component_scores)
        proba = self._model.predict_proba(X)[0]
        win_index = list(self._model.classes_).index(1)
        return round(float(proba[win_index]), 4)

    def explain(self, component_scores: Dict[str, float]) -> Dict[str, float]:
        if not self.is_ready:
            return {}
        import shap

        background = np.array(self._background[-50:])
        explainer = shap.LinearExplainer(self._model, background)
        X = _vectorize(component_scores)
        shap_values = explainer.shap_values(X)[0]
        return {
            feature: round(float(value), 4)
            for feature, value in zip(FEATURE_ORDER, shap_values)
        }

    def save_state(self, path: str | None = None) -> None:
        target = Path(path or self._state_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        with tmp.open("wb") as handle:
            pickle.dump(
                {
                    "model": self._model,
                    "n_samples_seen": self._n_samples_seen,
                    "classes_seen": self._classes_seen,
                    "background": self._background,
                },
                handle,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        tmp.replace(target)

    def load_state(self, path: str | None = None) -> bool:
        target = Path(path or self._state_path)
        if not target.exists():
            return False
        with target.open("rb") as handle:
            state = pickle.load(handle)
        self._model = state["model"]
        self._n_samples_seen = int(state.get("n_samples_seen", 0))
        self._classes_seen = set(state.get("classes_seen", set()))
        self._background = list(state.get("background", []))
        return True
