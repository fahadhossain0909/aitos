"""Online neural value-function scorer with durable checkpoints."""

from __future__ import annotations

import os
import pickle
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
from sklearn.neural_network import MLPRegressor

from aitos.intelligence.rl_policy import RLPolicyScorer
from aitos.xai.ml_explainer import FEATURE_ORDER

DEFAULT_MIN_SAMPLES_FOR_CONFIDENCE = 30
DEFAULT_REWARD_SCALE_R_MULTIPLES = 2.0


def _vectorize(context: Dict[str, Any]) -> np.ndarray:
    return np.array(
        [[float(context.get(f, 5.0)) / 10.0 for f in FEATURE_ORDER]], dtype=float
    )


class DeepValueRLScorer(RLPolicyScorer):
    """Online neural value approximation trained continuously from outcomes.

    Durable updates use an OS-level file lock and reload the latest checkpoint
    before applying an update. This prevents the paper/live feedback loop and
    the historical-learning worker from silently overwriting each other's
    learning when they share the persistent model volume.
    """

    def __init__(
        self,
        hidden_layer_sizes: Tuple[int, ...] = (8,),
        learning_rate_init: float = 0.01,
        min_samples_for_confidence: int = DEFAULT_MIN_SAMPLES_FOR_CONFIDENCE,
        reward_scale_r_multiples: float = DEFAULT_REWARD_SCALE_R_MULTIPLES,
        random_state: int = 0,
        state_path: str = "models/online_rl/deep_value.pkl",
    ) -> None:
        self._model = MLPRegressor(
            hidden_layer_sizes=hidden_layer_sizes,
            activation="tanh",
            solver="sgd",
            learning_rate="constant",
            learning_rate_init=learning_rate_init,
            max_iter=1,
            random_state=random_state,
        )
        self._min_samples = min_samples_for_confidence
        self._reward_scale = reward_scale_r_multiples
        self._n_samples_seen = 0
        self._recent_rewards: List[float] = []
        self._state_path = Path(state_path)

    @property
    def n_samples_seen(self) -> int:
        return self._n_samples_seen

    @property
    def is_fitted(self) -> bool:
        return self._n_samples_seen > 0

    def update(
        self, symbol: str, context: Dict[str, Any], reward_r_multiple: float
    ) -> None:
        self._model.partial_fit(_vectorize(context), np.array([reward_r_multiple]))
        self._n_samples_seen += 1
        self._recent_rewards.append(reward_r_multiple)
        if len(self._recent_rewards) > 500:
            self._recent_rewards.pop(0)

    def update_and_persist(
        self, symbol: str, context: Dict[str, Any], reward_r_multiple: float
    ) -> None:
        """Atomically merge one outcome into the shared persistent model."""
        target = self._state_path
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.with_suffix(target.suffix + ".lock")
        with lock_path.open("a+b") as lock_handle:
            try:
                import fcntl

                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            except ImportError:
                pass
            try:
                self.load_state(str(target))
                self.update(symbol, context, reward_r_multiple)
                self.save_state(str(target))
            finally:
                try:
                    import fcntl

                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                except ImportError:
                    pass

    async def score(self, symbol: str, context: Dict[str, Any]) -> float:
        if not self.is_fitted:
            return 5.0
        predicted_reward = float(self._model.predict(_vectorize(context))[0])
        raw_score = (
            5.0 + max(-1.0, min(1.0, predicted_reward / self._reward_scale)) * 5.0
        )
        confidence = min(1.0, self._n_samples_seen / self._min_samples)
        return round(max(0.0, min(10.0, 5.0 + (raw_score - 5.0) * confidence)), 2)

    def save_state(self, path: str | None = None) -> None:
        target = Path(path or self._state_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
        with tmp.open("wb") as handle:
            pickle.dump(
                {
                    "model": self._model,
                    "n_samples_seen": self._n_samples_seen,
                    "recent_rewards": self._recent_rewards,
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
            state = pickle.load(handle)  # nosec B301 - state is generated and stored locally by AITOS
        self._model = state["model"]
        self._n_samples_seen = int(state.get("n_samples_seen", 0))
        self._recent_rewards = list(state.get("recent_rewards", []))
        return True
