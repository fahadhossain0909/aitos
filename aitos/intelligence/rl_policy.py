"""RL policy confidence — spec section 32.1's "RL confidence (policy
value)" dimension.

No trained RL policy exists yet in this codebase (that's a Learning
Engine phase of its own — data collection, training, backtesting,
promotion gates). ``RLPolicyScorer`` is the seam a real one plugs into;
``NeutralRLScorer`` is a documented placeholder that always returns a
neutral 5.0 so the scanner's composite score is well-defined today
without silently overweighting a dimension nobody has actually modeled.
"""

from __future__ import annotations

import os
import pickle
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class RLPolicyScorer(ABC):
    @abstractmethod
    async def score(self, symbol: str, context: dict[str, Any]) -> float:
        """Return the RL policy's confidence in this symbol/context, 0-10."""


class NeutralRLScorer(RLPolicyScorer):
    async def score(self, symbol: str, context: dict[str, Any]) -> float:
        return 5.0


class TabularBanditRLScorer(RLPolicyScorer):
    """A real, working (if intentionally simple) RL policy: a tabular
    contextual bandit keyed on (symbol, regime, direction), trained
    online via ``update()`` as trades close.

    This is deliberately not a deep RL agent — no neural network, no
    replay buffer, no policy gradient. It's the simplest thing that
    actually learns from real outcomes rather than staying neutral
    forever: each bucket tracks a running mean reward (realized
    R-multiple), and ``score`` maps that mean through a bounded transform
    to the same 0-10 scale every other scorer uses. Buckets with no data
    yet return a neutral 5.0 — cold start behaves exactly like
    ``NeutralRLScorer`` until real trades accumulate.

    Wire ``RLFeedbackLoop`` (below) to call ``update`` automatically from
    ``trade.position_closed`` events; nothing calls it on its own.
    """

    def __init__(
        self,
        reward_scale_r_multiples: float = 2.0,
        min_samples_for_confidence: int = 5,
        state_path: str = "/models/online_rl/tabular_bandit.pkl",
    ) -> None:
        """``reward_scale_r_multiples`` sets how many R of average reward
        maps to the extreme ends of the 0-10 scale (e.g. 2.0 means an
        average of +2R maps to ~10, -2R maps to ~0). Buckets with fewer
        than ``min_samples_for_confidence`` observations get their score
        pulled partway back toward neutral (5.0) — a small sample's mean
        shouldn't be trusted as much as a large one's."""
        self._reward_scale = reward_scale_r_multiples
        self._min_samples = min_samples_for_confidence
        self._counts: dict[tuple[str, str, str], int] = {}
        self._means: dict[tuple[str, str, str], float] = {}
        self._state_path = Path(state_path)

    def _key(self, symbol: str, regime: str, direction: str) -> tuple[str, str, str]:
        return (symbol, regime, direction)

    def update(
        self, symbol: str, context: dict[str, Any], reward_r_multiple: float
    ) -> None:
        """Incorporate one real trade outcome. Uses Welford's incremental
        mean update — no need to store the full history per bucket.
        ``context`` must contain ``regime`` and ``direction`` (same shape
        ``score``'s context uses) — this signature matches
        ``DeepValueRLScorer.update`` so ``RLFeedbackLoop`` can train either
        scorer without caring which one it has."""
        regime = str(context.get("regime", "unknown"))
        direction = str(context.get("direction", "unknown"))
        key = self._key(symbol, regime, direction)
        count = self._counts.get(key, 0) + 1
        previous_mean = self._means.get(key, 0.0)
        new_mean = previous_mean + (reward_r_multiple - previous_mean) / count
        self._counts[key] = count
        self._means[key] = new_mean

    def sample_count(self, symbol: str, regime: str, direction: str) -> int:
        return self._counts.get(self._key(symbol, regime, direction), 0)

    async def score(self, symbol: str, context: dict[str, Any]) -> float:
        regime = str(context.get("regime", "unknown"))
        direction = str(context.get("direction", "unknown"))
        key = self._key(symbol, regime, direction)
        count = self._counts.get(key, 0)
        if count == 0:
            return 5.0

        mean_reward = self._means[key]
        raw_score = 5.0 + max(-1.0, min(1.0, mean_reward / self._reward_scale)) * 5.0

        # Shrink toward neutral for low-confidence (small-sample) buckets.
        confidence = min(1.0, count / self._min_samples)
        blended = 5.0 + (raw_score - 5.0) * confidence
        return round(max(0.0, min(10.0, blended)), 2)

    def save_state(self, path: str | None = None) -> None:
        target = Path(path or self._state_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
        payload = {
            "counts": {",".join(k): v for k, v in self._counts.items()},
            "means": {",".join(k): v for k, v in self._means.items()},
        }
        with tmp.open("wb") as handle:
            pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(target)

    def load_state(self, path: str | None = None) -> bool:
        target = Path(path or self._state_path)
        if not target.exists():
            return False
        with target.open("rb") as handle:
            state = pickle.load(
                handle
            )  # nosec B301 - state is generated and stored locally by AITOS
        counts: dict[tuple[str, str, str], int] = {}
        means: dict[tuple[str, str, str], float] = {}
        for raw_key, value in dict(state.get("counts", {})).items():
            parts = tuple(str(raw_key).split(","))
            if len(parts) != 3:
                continue
            counts[parts] = int(value)
        for raw_key, value in dict(state.get("means", {})).items():
            parts = tuple(str(raw_key).split(","))
            if len(parts) != 3:
                continue
            means[parts] = float(value)
        self._counts = counts
        self._means = means
        return True

    def update_and_persist(
        self, symbol: str, context: dict[str, Any], reward_r_multiple: float
    ) -> None:
        """Atomically merge one outcome into the shared persistent table."""
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
