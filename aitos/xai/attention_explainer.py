"""AttentionExplainer — spec section 33.2's "attention visualization" technique.

A genuine (if small) self-attention network, built from scratch with
plain numpy - no PyTorch/TensorFlow dependency, since a single-head
attention layer over 10 scalar features doesn't need a full deep learning
framework, and pulling one in for this would be a heavy, disproportionate
dependency for a project that otherwise stays lean.

Trained online (one gradient step per real closed trade) via numerical
differentiation (central finite differences) rather than hand-derived
analytical backpropagation. This is a deliberate correctness choice: for
a small model (~130 parameters) trained one sample at a time, the speed
cost of numerical gradients is negligible, and it sidesteps the real risk
of a subtle sign/transpose error in hand-written attention backprop
silently producing wrong-but-plausible-looking explanations - which would
be worse than not having this feature at all for something whose whole
point is trustworthiness.

Architecture: each of the 10 component scores is treated as one "token".
Each token is embedded (per-position learned scale + bias, so token
identity is baked into which position sees which weights), then standard
scaled dot-product self-attention (Q/K/V projections, softmax,
weighted sum), mean-pooled, and projected to a win-probability logit.

attention_weights() returns, per feature, how much the model's single
attention query weighs that feature when forming this particular
prediction - the actual "visualization" data. Note: like attention
mechanisms generally, weight direction doesn't always match naive
intuition (a feature can get very high attention specifically when it
signals one outcome and very low attention when it signals the other,
rather than "important features always get more attention") - see the
test suite's test_attention_responds_to_the_informative_feature_value
for what this model actually does with a clear synthetic pattern.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from aitos.xai.ml_explainer import FEATURE_ORDER

N_FEATURES = len(FEATURE_ORDER)
D_MODEL = 4

DEFAULT_MIN_SAMPLES_FOR_READY = 30
FINITE_DIFF_EPS = 1e-4


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


def _softmax(x: np.ndarray, axis: int = -1) -> np.ndarray:
    shifted = x - np.max(x, axis=axis, keepdims=True)
    exp = np.exp(shifted)
    return exp / np.sum(exp, axis=axis, keepdims=True)


def _vectorize(context: dict[str, Any]) -> np.ndarray:
    # Centered to [-1, 1] around the neutral score of 5.0 — a feature sitting
    # at the neutral value contributes exactly 0 to its token (x * W_embed = 0),
    # so only genuinely informative (non-neutral) features produce any signal;
    # scaling to [0, 1] instead left every "neutral" feature still contributing
    # a nonzero baseline term that diluted the signal from the features that
    # actually mattered (verified empirically to make training far slower).
    return np.array(
        [(float(context.get(f, 5.0)) - 5.0) / 5.0 for f in FEATURE_ORDER], dtype=float
    )


class AttentionExplainer:
    def __init__(
        self,
        d_model: int = D_MODEL,
        learning_rate: float = 2.0,
        min_samples_for_ready: int = DEFAULT_MIN_SAMPLES_FOR_READY,
        replay_buffer_size: int = 200,
        batch_size: int = 8,
        random_state: int = 0,
    ) -> None:
        self._d_model = d_model
        self._lr = learning_rate
        self._min_samples = min_samples_for_ready
        self._n_samples_seen = 0
        self._classes_seen: set = set()
        self._replay_buffer_size = replay_buffer_size
        self._batch_size = batch_size
        self._replay_buffer: list = []  # List[Tuple[np.ndarray, float]]
        self._rng = np.random.default_rng(random_state)

        scale = 0.3
        self._params: dict[str, np.ndarray] = {
            "W_embed": self._rng.normal(0, scale, (N_FEATURES, d_model)),
            "b_embed": self._rng.normal(0, scale, (N_FEATURES, d_model)),
            "q_cls": self._rng.normal(
                0, scale, (d_model,)
            ),  # single learned query (CLS-token style)
            "W_k": self._rng.normal(0, scale, (d_model, d_model)),
            "W_v": self._rng.normal(0, scale, (d_model, d_model)),
            "W_out": self._rng.normal(0, scale, (d_model,)),
            "b_out": np.zeros(1),
        }
        self._param_keys = list(self._params.keys())

    @property
    def n_samples_seen(self) -> int:
        return self._n_samples_seen

    @property
    def is_ready(self) -> bool:
        return self._n_samples_seen >= self._min_samples and self._classes_seen == {
            0,
            1,
        }

    # -- Forward pass -------------------------------------------------------------

    def _forward(
        self, x: np.ndarray, params: dict[str, np.ndarray]
    ) -> tuple[float, np.ndarray]:
        tokens = x[:, None] * params["W_embed"] + params["b_embed"]  # (N, d)
        K = tokens @ params["W_k"]
        V = tokens @ params["W_v"]
        scores = (params["q_cls"] @ K.T) / np.sqrt(
            self._d_model
        )  # (N,) — one query against every key
        attn = _softmax(
            scores, axis=-1
        )  # (N,) — single distribution over the 10 feature tokens
        pooled = attn @ V  # (d,) — weighted sum, no further pooling/dilution
        logit = float(pooled @ params["W_out"] + params["b_out"][0])
        return _sigmoid(logit), attn

    def predict_win_probability(self, context: dict[str, Any]) -> float | None:
        if not self.is_ready:
            return None
        x = _vectorize(context)
        prob, _ = self._forward(x, self._params)
        return round(prob, 4)

    def attention_weights(self, context: dict[str, Any]) -> dict[str, float]:
        """How much the model's single attention query weighs each of the
        10 feature tokens when forming this prediction — the actual
        "visualization" data. Sums to 1.0. Empty until ``is_ready``, same
        honesty gate as ``TradeOutcomeClassifier``."""
        if not self.is_ready:
            return {}
        x = _vectorize(context)
        _, attn = self._forward(x, self._params)
        return {feature: round(float(w), 4) for feature, w in zip(FEATURE_ORDER, attn)}

    # -- Training (numerical gradient descent) -----------------------------------

    def partial_fit(self, context: dict[str, Any], won: bool) -> None:
        x = _vectorize(context)
        y = 1.0 if won else 0.0

        self._replay_buffer.append((x, y))
        if len(self._replay_buffer) > self._replay_buffer_size:
            self._replay_buffer.pop(0)

        batch_size = min(self._batch_size, len(self._replay_buffer))
        batch_indices = self._rng.choice(
            len(self._replay_buffer), size=batch_size, replace=False
        )
        batch = [self._replay_buffer[i] for i in batch_indices]

        averaged_grads = {
            key: np.zeros_like(self._params[key]) for key in self._param_keys
        }
        for sample_x, sample_y in batch:
            sample_grads = self._numerical_gradient(sample_x, sample_y)
            for key in self._param_keys:
                averaged_grads[key] += sample_grads[key] / batch_size

        for key in self._param_keys:
            self._params[key] = self._params[key] - self._lr * averaged_grads[key]

        self._n_samples_seen += 1
        self._classes_seen.add(int(y))

    def _loss(self, x: np.ndarray, y: float, params: dict[str, np.ndarray]) -> float:
        prob, _ = self._forward(x, params)
        prob = min(max(prob, 1e-9), 1 - 1e-9)  # avoid log(0)
        return -(y * np.log(prob) + (1 - y) * np.log(1 - prob))

    def _numerical_gradient(self, x: np.ndarray, y: float) -> dict[str, np.ndarray]:
        grads: dict[str, np.ndarray] = {}
        for key in self._param_keys:
            param = self._params[key]
            grad = np.zeros_like(param)
            it = np.nditer(param, flags=["multi_index"])
            for _ in it:
                idx = it.multi_index
                original = param[idx]

                param[idx] = original + FINITE_DIFF_EPS
                loss_plus = self._loss(x, y, self._params)
                param[idx] = original - FINITE_DIFF_EPS
                loss_minus = self._loss(x, y, self._params)
                param[idx] = original

                grad[idx] = (loss_plus - loss_minus) / (2 * FINITE_DIFF_EPS)
            grads[key] = grad
        return grads
