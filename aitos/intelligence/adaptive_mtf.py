"""Adaptive multi-timeframe decision support and quant validation primitives.

This module is deliberately pure: it does not fetch data, place orders, mutate
positions, or enable live trading. Callers supply completed candles and evidence.
All directional calculations are symmetric and return auditable diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Iterable, Mapping

from aitos.models.trade import TradeSide


@dataclass(frozen=True, slots=True)
class DirectionalEvidence:
    """Normalized evidence where 0 is strongly short and 10 strongly long."""

    structure: float = 5.0
    order_flow: float = 5.0
    momentum: float = 5.0
    liquidity: float = 5.0
    location: float = 5.0
    participation: float = 5.0

    def __post_init__(self) -> None:
        for name, value in self.as_dict().items():
            if not isfinite(value) or not 0.0 <= value <= 10.0:
                raise ValueError(f"{name} must be finite and within [0, 10]")

    def as_dict(self) -> dict[str, float]:
        return {
            "structure": self.structure,
            "order_flow": self.order_flow,
            "momentum": self.momentum,
            "liquidity": self.liquidity,
            "location": self.location,
            "participation": self.participation,
        }

    def score_for(self, side: TradeSide) -> float:
        values = self.as_dict().values()
        long_score = sum(values) / len(self.as_dict())
        return round(long_score if side == TradeSide.LONG else 10.0 - long_score, 4)


@dataclass(frozen=True, slots=True)
class TimeframeContext:
    timeframe: str
    bias: str
    strength: float
    regime: str = "unknown"
    available: bool = True
    completed: bool = True
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.bias not in {"bullish", "bearish", "neutral", "unknown"}:
            raise ValueError("bias must be bullish, bearish, neutral, or unknown")
        if not isfinite(self.strength) or not 0.0 <= self.strength <= 10.0:
            raise ValueError("strength must be finite and within [0, 10]")


@dataclass(frozen=True, slots=True)
class HTFDecision:
    accepted: bool
    mode: str
    confidence_adjustment: float
    reason: str
    diagnostics: Mapping[str, Any]


def evaluate_adaptive_htf(
    side: TradeSide,
    contexts: Iterable[TimeframeContext],
    *,
    ltf: DirectionalEvidence,
    min_aligned_score: float = 4.0,
    countertrend_score: float = 7.0,
    alignment_balance_threshold: float = 0.25,
    allow_neutral: bool = True,
    require_any_context: bool = True,
) -> HTFDecision:
    """Evaluate HTF context without a brittle all-timeframes-must-agree gate.

    Missing/incomplete context is never silently treated as neutral. A valid
    countertrend setup needs strong LTF reversal evidence; aligned contexts
    boost confidence, while mixed/neutral contexts are conditional.
    """
    if not 0.0 < alignment_balance_threshold <= 1.0:
        raise ValueError("alignment_balance_threshold must be within (0, 1]")
    frames = tuple(contexts)
    valid = tuple(c for c in frames if c.available and c.completed)
    invalid = tuple(c.timeframe for c in frames if not c.available or not c.completed)
    if require_any_context and not valid:
        return HTFDecision(
            False,
            "unavailable",
            0.0,
            "htf_context_unavailable",
            {
                "available_timeframes": [],
                "invalid_timeframes": list(invalid),
            },
        )

    directional = [c for c in valid if c.bias in {"bullish", "bearish"}]
    aligned = [
        c
        for c in directional
        if (c.bias == "bullish" and side == TradeSide.LONG)
        or (c.bias == "bearish" and side == TradeSide.SHORT)
    ]
    opposed = [c for c in directional if c not in aligned]
    neutral = [c for c in valid if c.bias in {"neutral", "unknown"}]
    weights = [max(0.25, c.strength / 10.0) for c in directional]
    signed = [(1.0 if c in aligned else -1.0) * w for c, w in zip(directional, weights)]
    trend_balance = sum(signed) / sum(weights) if weights else 0.0
    ltf_score = ltf.score_for(side)
    reversal_evidence = (
        ltf.structure >= 6.5 and ltf.order_flow >= 6.5
        if side == TradeSide.LONG
        else ltf.structure <= 3.5 and ltf.order_flow <= 3.5
    )
    details = {
        "trend_balance": round(trend_balance, 4),
        "aligned": [c.timeframe for c in aligned],
        "opposed": [c.timeframe for c in opposed],
        "neutral": [c.timeframe for c in neutral],
        "ltf_score": ltf_score,
        "invalid_timeframes": list(invalid),
        "alignment_balance_threshold": alignment_balance_threshold,
    }
    # Weighted consensus avoids requiring every timeframe to agree.
    if trend_balance >= alignment_balance_threshold:
        return HTFDecision(
            True, "aligned", round(min(1.0, trend_balance), 4),
            "htf_weighted_consensus_aligned", details,
        )
    if trend_balance <= -alignment_balance_threshold:
        if ltf_score >= countertrend_score and reversal_evidence:
            return HTFDecision(
                True, "countertrend_reversal", -0.5,
                "strong_ltf_reversal_against_htf",
                {**details, "reversal_evidence": True},
            )
        return HTFDecision(
            False, "countertrend", 0.0,
            "countertrend_without_confirmed_reversal",
            {**details, "reversal_evidence": bool(reversal_evidence)},
        )
    if allow_neutral and ltf_score >= min_aligned_score:
        return HTFDecision(
            True, "neutral_or_mixed", 0.0,
            "htf_mixed_ltf_quality_sufficient", details,
        )
    return HTFDecision(
        False, "neutral_or_mixed", 0.0,
        "ltf_quality_below_neutral_floor" if allow_neutral else "neutral_context_disabled",
        details,
    )


def directional_diagnostics(
    structure_direction: str, cvd_score: float, *, structure_bias: str | None = None
) -> dict[str, Any]:
    """Explain both directional opportunities with mirror-symmetric thresholds."""
    if not isfinite(cvd_score):
        raise ValueError("cvd_score must be finite")
    bias = (structure_bias or "neutral").lower()
    long_structure = structure_direction == "bullish_bos"
    short_structure = structure_direction == "bearish_bos"
    long_flow = cvd_score >= 6.5
    short_flow = cvd_score <= 3.5
    long_bias_conflict = bias == "bearish"
    short_bias_conflict = bias == "bullish"
    long_structure_pass = long_structure or structure_direction == "none"
    short_structure_pass = short_structure or structure_direction == "none"
    long_pass = long_structure_pass and long_flow and not long_bias_conflict
    short_pass = short_structure_pass and short_flow and not short_bias_conflict

    def reasons(structure_ok: bool, flow_ok: bool, bias_conflict: bool) -> list[str]:
        result = []
        if not structure_ok:
            result.append("structure_not_confirmed")
        if not flow_ok:
            result.append("cvd_below_directional_threshold")
        if bias_conflict:
            result.append("higher_structure_bias_conflict")
        return result

    return {
        "long": {
            "structure_pass": long_structure_pass,
            "flow_pass": long_flow,
            "bias_conflict": long_bias_conflict,
            "accepted_by_diagnostic": long_pass,
            "rejection_reasons": reasons(
                long_structure_pass, long_flow, long_bias_conflict
            ),
        },
        "short": {
            "structure_pass": short_structure_pass,
            "flow_pass": short_flow,
            "bias_conflict": short_bias_conflict,
            "accepted_by_diagnostic": short_pass,
            "rejection_reasons": reasons(
                short_structure_pass, short_flow, short_bias_conflict
            ),
        },
        "cvd_score": cvd_score,
        "thresholds": {"long_min": 6.5, "short_max": 3.5},
    }


@dataclass(frozen=True, slots=True)
class ExpectedValue:
    expectancy_r: float
    win_probability: float
    reward_risk: float
    costs_r: float
    net_expectancy_r: float


def estimate_net_expectancy(
    win_probability: float, reward_risk: float, *, costs_r: float = 0.0
) -> ExpectedValue:
    """Estimate net R expectancy; inputs must be out-of-sample calibrated."""
    if not 0.0 <= win_probability <= 1.0:
        raise ValueError("win_probability must be within [0, 1]")
    if reward_risk <= 0 or costs_r < 0:
        raise ValueError("reward_risk must be positive and costs_r non-negative")
    gross = win_probability * reward_risk - (1.0 - win_probability)
    return ExpectedValue(gross, win_probability, reward_risk, costs_r, gross - costs_r)


def volatility_risk_multiplier(percentile: float, *, floor: float = 0.25) -> float:
    """Smooth risk dampener; never increases the configured base risk."""
    if not 0.0 <= percentile <= 100.0 or not 0.0 < floor <= 1.0:
        raise ValueError("invalid volatility percentile or floor")
    return round(max(floor, 1.0 - 0.75 * percentile / 100.0), 4)


def portfolio_directional_exposure(
    positions: Iterable[Mapping[str, Any]],
) -> dict[str, float]:
    """Aggregate gross and signed notional; malformed positions fail safely."""
    long_usd = short_usd = 0.0
    for position in positions:
        side = str(position.get("side", "")).upper()
        notional = float(position.get("notional_usd", 0.0))
        if not isfinite(notional) or notional < 0:
            raise ValueError("position notional must be finite and non-negative")
        if side == "LONG":
            long_usd += notional
        elif side == "SHORT":
            short_usd += notional
        else:
            raise ValueError(f"unsupported position side: {side}")
    return {
        "long_notional_usd": round(long_usd, 8),
        "short_notional_usd": round(short_usd, 8),
        "gross_notional_usd": round(long_usd + short_usd, 8),
        "net_directional_usd": round(long_usd - short_usd, 8),
    }


def walk_forward_metrics(
    trades: Iterable[Mapping[str, Any]],
) -> dict[str, float | None]:
    """Compute transparent trade-level diagnostics from realized net PnL rows."""
    pnl = [float(row["net_pnl"]) for row in trades]
    if any(not isfinite(value) for value in pnl):
        raise ValueError("net_pnl values must be finite")
    wins = [value for value in pnl if value > 0]
    losses = [-value for value in pnl if value < 0]
    gross_profit, gross_loss = sum(wins), sum(losses)
    equity = peak = 0.0
    max_drawdown = 0.0
    for value in pnl:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    count = len(pnl)
    return {
        "trade_count": float(count),
        "win_rate": len(wins) / count if count else 0.0,
        "profit_factor": (
            gross_profit / gross_loss if gross_loss else (None if gross_profit else 0.0)
        ),
        "expectancy": sum(pnl) / count if count else 0.0,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "max_drawdown": max_drawdown,
    }
