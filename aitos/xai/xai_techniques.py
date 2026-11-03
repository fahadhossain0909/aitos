"""Spec §33.2 lists five XAI techniques. Four are implemented in this
codebase; one genuinely doesn't apply to this domain:

- **Saliency maps** — a CNN-specific technique for spatial/image
  gradients. This system has no image or spatial data anywhere in its
  pipeline (market data is tabular/time-series, not visual), so there's
  no natural input to attach a saliency map to. Rather than force an
  artificial mapping (e.g. treating a price chart as an "image"), this
  is left explicitly out of scope as inapplicable, not merely unbuilt.

Implemented today:
- **Feature importance (SHAP)** — see ``ml_explainer.py``'s
  ``TradeOutcomeClassifier``: an online logistic classifier trained
  incrementally from real closed-trade outcomes (via
  ``MLExplainerFeedbackLoop`` subscribing to ``trade.position_closed`` —
  no manual training step needed), explained with
  ``shap.LinearExplainer`` once enough trades have accumulated
  (``is_ready``).
- **Attention visualization** — see ``attention_explainer.py``'s
  ``AttentionExplainer``: a genuine single-head self-attention network
  built from scratch with numpy (no PyTorch dependency — the model is
  small enough not to need one), trained online via
  ``AttentionFeedbackLoop``. ``attention_weights()`` returns which of the
  scanner's ten dimensions the model's attention query weighed most when
  forming a prediction. Caveat, documented in the module itself and
  demonstrated in its tests: attention weight direction doesn't always
  match naive "important = high attention" intuition — a well-known
  finding in attention-interpretability research, not a bug here.
- **Counterfactual explanations** ("what would change the decision") —
  see ``counterfactual.py``, computed directly from the Opportunity
  Scanner's component scores without needing a trained model.
- **Natural language generation from structured explanations** — see
  ``build_trade_explanation`` in ``explanation.py``.

Before any of ``TradeOutcomeClassifier``/``AttentionExplainer`` reaches
``is_ready`` (both outcome classes observed, minimum 30 samples by
default), their explain methods return an empty result rather than a
misleadingly confident one from a barely-trained model — small-sample
explanations are noise, not insight.
"""
