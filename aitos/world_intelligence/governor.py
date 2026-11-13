"""Fail-safe policy boundary for advisory world-intelligence context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    status: str
    allow_intelligence_adjustment: bool
    recommend_new_entry_block: bool
    exposure_multiplier: float
    reasons: tuple[str, ...]


class IntelligenceRiskGovernor:
    """Emits advisory restrictions; cannot override the core risk engine."""

    def __init__(
        self, *, max_age_seconds: float = 300.0, critical_severity: float = 0.85
    ):
        if max_age_seconds <= 0 or not 0.0 <= critical_severity <= 1.0:
            raise ValueError("invalid governor configuration")
        self.max_age_seconds = float(max_age_seconds)
        self.critical_severity = float(critical_severity)

    def assess(
        self,
        *,
        available: bool,
        observed_at: datetime | None,
        verified: bool,
        severity: float | None,
        uncertainty: float | None = None,
        now: datetime | None = None,
    ) -> RiskAssessment:
        timestamp = now or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if not available or observed_at is None:
            return RiskAssessment(
                "unavailable",
                False,
                False,
                1.0,
                ("intelligence_unavailable_core_policy_unchanged",),
            )
        if observed_at.tzinfo is None:
            return RiskAssessment(
                "invalid", False, False, 1.0, ("naive_timestamp_rejected",)
            )
        age = (
            timestamp.astimezone(timezone.utc)
            - observed_at.astimezone(timezone.utc)
        ).total_seconds()
        if age < -5.0 or age > self.max_age_seconds:
            return RiskAssessment(
                "stale", False, False, 1.0, ("stale_or_future_intelligence",)
            )
        if severity is None or not isfinite(float(severity)) or not 0.0 <= float(
            severity
        ) <= 1.0:
            return RiskAssessment("invalid", False, False, 1.0, ("invalid_severity",))
        if uncertainty is not None and (
            not isfinite(float(uncertainty)) or not 0.0 <= float(uncertainty) <= 1.0
        ):
            return RiskAssessment(
                "invalid", False, False, 1.0, ("invalid_uncertainty",)
            )
        if not verified:
            return RiskAssessment(
                "unverified",
                False,
                False,
                1.0,
                ("unverified_event_is_alert_only",),
            )
        if float(severity) >= self.critical_severity:
            return RiskAssessment(
                "critical",
                True,
                True,
                0.5,
                (
                    "verified_fresh_event",
                    "critical_event_requires_core_risk_review",
                ),
            )
        if uncertainty is not None and float(uncertainty) >= 0.8:
            return RiskAssessment(
                "uncertain",
                True,
                False,
                0.75,
                (
                    "verified_fresh_event",
                    "high_uncertainty_recommends_confirmation",
                ),
            )
        return RiskAssessment("normal", True, False, 1.0, ("verified_fresh_event",))
