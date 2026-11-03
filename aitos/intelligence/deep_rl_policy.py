"""Online neural value-function scorer with durable checkpoints."""

from __future__ import annotations

import os
from contextlib import contextmanager
import pickle
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.neural_network import MLPRegressor

from aitos.config.paths import get_model_data_dir
from aitos.intelligence.rl_policy import RLPolicyScorer
from aitos.xai.ml_explainer import FEATURE_ORDER

DEFAULT_MIN_SAMPLES_FOR_CONFIDENCE = 30
DEFAULT_REWARD_SCALE_R_MULTIPLES = 2.0
DEFAULT_MODEL_DATA_DIR = str(get_model_data_dir())


@contextmanager
def _exclusive_model_lock(handle):
    """Cross-platform exclusive lock for shared model checkpoint writers."""
    if os.name == "nt":
        import msvcrt
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _vectorize(context: dict[str, Any]) -> np.ndarray:
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
        hidden_layer_sizes: tuple[int, ...] = (8,),
        learning_rate_init: float = 0.01,
        min_samples_for_confidence: int = DEFAULT_MIN_SAMPLES_FOR_CONFIDENCE,
        reward_scale_r_multiples: float = DEFAULT_REWARD_SCALE_R_MULTIPLES,
        random_state: int = 0,
        state_path: str | None = None,
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
        self._recent_rewards: list[float] = []
        self._applied_experience_ids: set[str] = set()
        self._applied_experience_order: list[str] = []
        model_data_dir = get_model_data_dir()
        self._state_path = (
            Path(state_path)
            if state_path
            else (model_data_dir / "online_rl" / "deep_value.pkl")
        )

    @property
    def n_samples_seen(self) -> int:
        return self._n_samples_seen

    @property
    def is_fitted(self) -> bool:
        return self._n_samples_seen > 0

    def update(
        self, symbol: str, context: dict[str, Any], reward_r_multiple: float
    ) -> None:
        self._model.partial_fit(_vectorize(context), np.array([reward_r_multiple]))
        self._n_samples_seen += 1
        self._recent_rewards.append(reward_r_multiple)
        if len(self._recent_rewards) > 500:
            self._recent_rewards.pop(0)

    def update_and_persist(
        self, symbol: str, context: dict[str, Any], reward_r_multiple: float
    ) -> None:
        """Atomically merge one outcome into the shared persistent model."""
        target = self._state_path
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.with_suffix(target.suffix + ".lock")
        with lock_path.open("a+b") as lock_handle:
            with _exclusive_model_lock(lock_handle):
                self.load_state(str(target))
                self.update(symbol, context, reward_r_multiple)
                self._save_state_unlocked(str(target))

    def update_experience_and_persist(
        self, experience_id: str, symbol: str, context: dict[str, Any], reward_r_multiple: float
    ) -> bool:
        """Apply a historical experience at most once across crashes/restarts."""
        if not experience_id:
            raise ValueError("experience_id is required for idempotent learning")
        target = self._state_path
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.with_suffix(target.suffix + ".lock")
        with lock_path.open("a+b") as lock_handle:
            with _exclusive_model_lock(lock_handle):
                self.load_state(str(target))
                if experience_id in self._applied_experience_ids:
                    return False
                self.update(symbol, context, reward_r_multiple)
                self._applied_experience_ids.add(experience_id)
                self._applied_experience_order.append(experience_id)
                if len(self._applied_experience_order) > 10000:
                    expired = self._applied_experience_order.pop(0)
                    self._applied_experience_ids.discard(expired)
                self.save_state(str(target))
                return True

    def update_trade_and_persist(
        self, trade_id: str, symbol: str, context: dict[str, Any], reward_r_multiple: float
    ) -> bool:
        """Apply a finalized trade outcome once, even if its event is redelivered."""
        if not trade_id:
            raise ValueError("trade_id is required for idempotent trade learning")
        return self.update_experience_and_persist(
            f"trade:{trade_id}", symbol, context, reward_r_multiple
        )

    async def score(self, symbol: str, context: dict[str, Any]) -> float:
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
        lock_path = target.with_suffix(target.suffix + ".lock")
        with lock_path.open("a+b") as lock_handle:
            with _exclusive_model_lock(lock_handle):
                self._save_state_unlocked(str(target))

    def _save_state_unlocked(self, path: str | None = None) -> None:
        target = Path(path or self._state_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
        with tmp.open("wb") as handle:
            pickle.dump(
                {
                    "model": self._model,
                    "n_samples_seen": self._n_samples_seen,
                    "recent_rewards": self._recent_rewards,
                    "applied_experience_ids": self._applied_experience_order[-10000:],
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
            state = pickle.load(
                handle
            )  # nosec B301 - state is generated and stored locally by AITOS
        self._model = state["model"]
        self._n_samples_seen = int(state.get("n_samples_seen", 0))
        self._recent_rewards = list(state.get("recent_rewards", []))
        self._applied_experience_order = list(state.get("applied_experience_ids", []))[-10000:]
        self._applied_experience_ids = set(self._applied_experience_order)
        return True
