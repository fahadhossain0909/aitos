"""Explainable AI (XAI) — spec section 33.

``TradeExplanation`` is the exact dataclass from spec §33.1.
``build_trade_explanation`` is real natural-language generation "from
structured explanations" (spec §33.2's NLG technique) — it doesn't call an
LLM or any model; it deterministically turns the structured data every
trade already carries (component scores from the Opportunity Scanner,
sizing rationale, risk breakdown) into the seven required narrative
fields. No trade should ever lack an explanation because some upstream
model didn't run — this always produces something from what's on hand.

The other four §33.2 techniques (SHAP/permutation feature importance,
attention visualization, saliency maps, counterfactuals) are genuinely
model-specific — they need a trained ML/DL model to explain, which
doesn't exist in this codebase yet. Adding them here would mean either
faking output or building a training pipeline; both are out of scope for
this module. They stay documented, not implemented, in `xai_techniques.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aitos.risk.models import RiskScoreBreakdown

# Component scores at/above this are cited as supporting evidence;
# at/below this (on the 0-10 scale) are cited as conflicting evidence.
SUPPORTING_THRESHOLD = 6.0
CONFLICTING_THRESHOLD = 4.0


@dataclass(frozen=True)
class TradeExplanation:
    """Spec §33.1 — every trade MUST explain these seven things."""

    why_trade: str
    why_now: str
    why_leverage: str
    why_sl: str
    why_tp: str
    confidence_score: float
    supporting_evidence: list[str] = field(default_factory=list)
    conflicting_evidence: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    agent_contributions: dict[str, str] = field(default_factory=dict)
    market_context: str = ""
    regime_context: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "why_trade": self.why_trade,
            "why_now": self.why_now,
            "why_leverage": self.why_leverage,
            "why_sl": self.why_sl,
            "why_tp": self.why_tp,
            "confidence_score": self.confidence_score,
            "supporting_evidence": self.supporting_evidence,
            "conflicting_evidence": self.conflicting_evidence,
            "risks": self.risks,
            "agent_contributions": self.agent_contributions,
            "market_context": self.market_context,
            "regime_context": self.regime_context,
        }

    def as_narrative(self) -> str:
        """A single human-readable paragraph, for logs/notifications."""
        lines = [
            f"Trade: {self.why_trade}",
            f"Timing: {self.why_now}",
            f"Leverage: {self.why_leverage}",
            f"Stop loss: {self.why_sl}",
            f"Take profit: {self.why_tp}",
            f"Confidence: {self.confidence_score:.0%}",
        ]
        if self.supporting_evidence:
            lines.append("Supporting: " + "; ".join(self.supporting_evidence))
        if self.conflicting_evidence:
            lines.append("Conflicting: " + "; ".join(self.conflicting_evidence))
        if self.risks:
            lines.append("Risks: " + "; ".join(self.risks))
        return " | ".join(lines)


def build_trade_explanation(
    trade_dict: dict[str, Any],
    risk_assessment: RiskScoreBreakdown | None = None,
    regime: str = "",
) -> TradeExplanation:
    """Build a ``TradeExplanation`` from a ``Trade.to_dict()`` payload.

    Works on the plain-dict form (rather than the ``Trade`` object itself)
    so it can be called from an event handler — the Journal System never
    needs a live reference to the Trade Lifecycle's in-memory trade, only
    the event payload.
    """
    symbol = trade_dict.get("symbol", "?")
    side = trade_dict.get("side", "?")
    strategy_id = trade_dict.get("strategy_id", "unknown-strategy")
    agent_consensus: dict[str, float] = trade_dict.get("agent_consensus", {}) or {}
    rationale = trade_dict.get("explanation", "") or ""

    why_trade = (
        f"{side} {symbol} selected by strategy '{strategy_id}'. {rationale}".strip()
    )

    triggering_signals = [
        k.replace("_", " ")
        for k, v in agent_consensus.items()
        if v >= SUPPORTING_THRESHOLD
    ]
    why_now = (
        f"Entry triggered on confluence of: {', '.join(triggering_signals)}."
        if triggering_signals
        else "Entry triggered by the strategy's signal conditions at the time of scan."
    )

    leverage = trade_dict.get("leverage", 0)
    why_leverage = (
        f"Leverage set to {leverage}x via the adaptive-leverage formula "
        f"(shrinks with volatility and current risk score; never exceeds the configured max)."
    )

    entry_price = trade_dict.get("entry_price", 0.0)
    sl_price = trade_dict.get("sl_price", 0.0)
    why_sl = (
        f"Stop loss at {sl_price} ({abs(entry_price - sl_price):.6g} from entry), "
        f"derived from ATR-based structure distance rather than a fixed pip amount."
    )

    tp_levels = trade_dict.get("take_profit_levels", [])
    why_tp = (
        f"Take-profit levels at {tp_levels} (R-multiple targets from the initial risk distance), "
        f"with partial exits at each intermediate level."
        if tp_levels
        else "No take-profit levels were set for this trade."
    )

    supporting_evidence = [
        f"{k.replace('_', ' ')} favorable ({v:.1f}/10)"
        for k, v in agent_consensus.items()
        if v >= SUPPORTING_THRESHOLD
    ]
    conflicting_evidence = [
        f"{k.replace('_', ' ')} unfavorable ({v:.1f}/10)"
        for k, v in agent_consensus.items()
        if v <= CONFLICTING_THRESHOLD
    ]

    risks: list[str] = []
    risk_amount = trade_dict.get("risk_amount_usd", 0.0)
    position_size = trade_dict.get("position_size_usd", 0.0)
    if position_size:
        risks.append(
            f"Risking ${risk_amount:.2f} ({risk_amount / position_size * 100:.2f}% of position notional) if stop is hit"
        )
    if risk_assessment is not None:
        risks.extend(risk_assessment.explanation)
        risks.append(
            f"Portfolio risk score at entry: {risk_assessment.total:.1f}/100 ({risk_assessment.action.value})"
        )

    confidence = 0.0
    if agent_consensus:
        confidence = round(
            sum(agent_consensus.values()) / (len(agent_consensus) * 10), 4
        )

    agent_contributions = {k: f"{v:.1f}/10" for k, v in agent_consensus.items()}

    return TradeExplanation(
        why_trade=why_trade,
        why_now=why_now,
        why_leverage=why_leverage,
        why_sl=why_sl,
        why_tp=why_tp,
        confidence_score=confidence,
        supporting_evidence=supporting_evidence,
        conflicting_evidence=conflicting_evidence,
        risks=risks,
        agent_contributions=agent_contributions,
        market_context=f"entry_price={entry_price}, position_size_usd={position_size}",
        regime_context=regime or "unknown",
    )
