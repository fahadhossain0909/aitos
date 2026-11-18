"""Safe adapter between scanner candidates and advisory ADL evaluations.

The bridge deliberately transports evidence without treating scanner scores as
calibrated probabilities. A configured provider must produce its own
probability distribution and the ADL runtime retains its fail-closed policy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aitos.intelligence.scanner import ScanCandidate

from .types import MarketSnapshot


def candidate_to_snapshot(candidate: ScanCandidate) -> MarketSnapshot:
    """Convert a scored candidate into an immutable, versioned ADL snapshot."""
    timestamp = datetime.fromisoformat(candidate.scanned_at.replace("Z", "+00:00"))
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return MarketSnapshot(
        symbol=candidate.symbol,
        timestamp=timestamp,
        snapshot_id=candidate.candidate_id,
        source_versions={"scanner": "candidate-v1"},
        features={
            "composite_score": float(candidate.composite_score),
                "trend": float(candidate.component_scores.get("trend_strength", 0.0)) / 10.0,
                "order_flow": float(candidate.component_scores.get("order_flow_bias", 0.0)) / 10.0,
                "liquidity": float(candidate.component_scores.get("liquidity_quality", 0.0)) / 10.0,
                "volatility": float(candidate.component_scores.get("volatility", 0.0)) / 10.0,
                "market_regime": float(candidate.component_scores.get("market_regime", 0.0)) / 10.0,
            "component_scores": dict(candidate.component_scores),
            "component_availability": dict(candidate.component_availability),
            "regime": candidate.regime,
            "direction": candidate.direction.value,
            "entry_price": float(candidate.entry_price),
            "atr": float(candidate.atr),
            "htf_adx": float(candidate.htf_adx),
            "financial_ml_features": dict(candidate.financial_ml_features),
            "statistical_forecast": dict(candidate.statistical_forecast or {}),
            "world_intelligence_shadow": dict(candidate.world_intelligence_shadow),
            "risk_veto": False,
            "execution_veto": False,
            "data_integrity_veto": False,
        },
    )


def decision_event_payload(candidate: ScanCandidate, result: Any) -> dict[str, Any]:
    """Build an auditable shadow event; it cannot authorize order execution."""
    return {
        "decision_id": result.decision_id,
        "candidate_id": candidate.candidate_id,
        "symbol": candidate.symbol,
        "scanner_direction": candidate.direction.value,
        "adl_action": result.action.value,
        "adl_proposed_action": result.proposed_action.value,
        "probabilities": dict(result.probabilities),
        "raw_probabilities": dict(result.raw_probabilities),
        "calibrated": bool(result.calibrated),
        "expected_value": result.expected_value,
        "evidence_disagreement": result.evidence_disagreement,
        "reasons": list(result.reasons),
        "model_id": result.model_id,
        "model_version": result.model_version,
        "snapshot_id": candidate.candidate_id,
        "trace_id": candidate.trace_id,
        "shadow_mode": True,
        "execution_eligible": False,
    }
