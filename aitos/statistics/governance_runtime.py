"""Opt-in live drift and champion/challenger governance controller."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from .governance import (
    PromotionDecision,
    PromotionEvidence,
    PromotionPolicy,
    evaluate_promotion,
)
from .registry import ModelArtifact, StatisticalModelRegistry
from .validation import DriftReport, assess_feature_drift

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DriftAction:
    report: DriftReport
    status: str
    promotion_blocked: bool
    reasons: tuple[str, ...]


class StatisticalGovernanceRuntime:
    """Runtime-safe drift assessment and explicit champion/challenger control.

    The caller supplies timestamp-aligned reference and current feature batches.
    Drift blocks promotion but does not autonomously close positions, disable
    execution, or replace the champion. Model promotion requires a second,
    explicit operator approval at this boundary.
    """

    def __init__(
        self,
        registry: StatisticalModelRegistry,
        *,
        policy: PromotionPolicy = PromotionPolicy(),
        drift_warning_threshold: float = 0.10,
        drift_critical_threshold: float = 0.25,
    ):
        if not 0 < drift_warning_threshold < drift_critical_threshold:
            raise ValueError("drift thresholds must be positive and ordered")
        self.registry = registry
        self.policy = policy
        self.drift_warning_threshold = drift_warning_threshold
        self.drift_critical_threshold = drift_critical_threshold
        self._last_drift: DriftAction | None = None

    @property
    def last_drift(self) -> DriftAction | None:
        return self._last_drift

    def mark_drift_unavailable(self, reason: str) -> DriftAction:
        """Fail closed when a fresh, valid drift assessment cannot be produced."""
        report = DriftReport(features=(), max_psi=0.0, drifted_features=())
        action = DriftAction(
            report=report,
            status="UNAVAILABLE",
            promotion_blocked=True,
            reasons=(str(reason) or "drift_data_unavailable",),
        )
        self._last_drift = action
        return action

    def observe_live_features(
        self,
        reference: Mapping[str, Sequence[float]],
        current: Mapping[str, Sequence[float]],
    ) -> DriftAction:
        report = assess_feature_drift(
            reference,
            current,
            threshold=self.drift_warning_threshold,
        )
        if report.max_psi >= self.drift_critical_threshold:
            status = "CRITICAL"
            reasons = ("critical_feature_drift",)
        elif report.drifted_features:
            status = "WARNING"
            reasons = ("feature_drift_detected",)
        else:
            status = "NORMAL"
            reasons = ()
        action = DriftAction(
            report=report,
            status=status,
            promotion_blocked=status != "NORMAL",
            reasons=reasons,
        )
        self._last_drift = action
        return action

    def evaluate_challenger(self, evidence: PromotionEvidence) -> PromotionDecision:
        decision = evaluate_promotion(evidence, self.policy)
        if self._last_drift is not None and self._last_drift.promotion_blocked:
            block_reason = (
                "live_feature_drift_blocks_promotion"
                if self._last_drift.status in {"WARNING", "CRITICAL"}
                else "live_drift_data_unavailable_blocks_promotion"
            )
            reasons = tuple(dict.fromkeys(decision.reasons + (block_reason,)))
            return PromotionDecision(False, reasons)
        return decision

    def promote_challenger(
        self,
        evidence: PromotionEvidence,
        *,
        operator_approved: bool = False,
    ) -> PromotionDecision:
        """Promote only when both evidence and this invocation carry approval."""
        approved = bool(operator_approved and evidence.operator_approved)
        candidate = replace(evidence, operator_approved=approved)
        decision = self.evaluate_challenger(candidate)
        if (
            not decision.eligible
            and self._last_drift is not None
            and self._last_drift.promotion_blocked
        ):
            candidate = replace(
                candidate,
                drift_score=max(
                    candidate.drift_score,
                    self.policy.max_drift_score + 1e-6,
                ),
            )
        # The registry independently re-evaluates the evidence and persists
        # both approved and blocked promotion attempts.
        return self.registry.promote(candidate, policy=self.policy)

    def rollback(
        self, model_id: str, target_version: str, *, operator_approved: bool = False
    ) -> ModelArtifact:
        return self.registry.rollback(
            model_id, target_version, operator_approved=operator_approved
        )


class DriftMonitorScheduler:
    """Async periodic drift orchestration with deduplicated operator alerts.

    The application must start run_forever from its managed service lifecycle.
    The scheduler never starts a hidden background thread at import/construct
    time, and it never changes execution policy or promotes models itself.
    """

    def __init__(
        self,
        runtime: StatisticalGovernanceRuntime,
        *,
        interval_seconds: float,
        alert_callback=None,
    ) -> None:
        import math

        if not math.isfinite(interval_seconds) or interval_seconds < 1.0:
            raise ValueError("drift interval_seconds must be finite and >= 1")
        self.runtime = runtime
        self.interval_seconds = float(interval_seconds)
        self.alert_callback = alert_callback
        self._last_alert_fingerprint: tuple | None = None
        self.last_run_at = None

    async def _notify(self, action: DriftAction) -> bool:
        """Deliver alerts without allowing a notification outage to kill monitoring."""
        import asyncio
        import inspect

        if self.alert_callback is None:
            return True
        try:
            callback = self.alert_callback
            is_async_callback = inspect.iscoroutinefunction(
                callback
            ) or inspect.iscoroutinefunction(getattr(callback, "__call__", None))
            if is_async_callback:
                await callback(action)
            else:
                result = await asyncio.to_thread(callback, action)
                if inspect.isawaitable(result):
                    await result
            return True
        except Exception:
            logger.exception("statistical drift alert delivery failed")
            return False

    async def run_once(self, reference, current, *, now=None):
        """Run one due check; return None when cadence has not elapsed."""
        from datetime import datetime, timezone

        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            raise ValueError("scheduler time must be timezone-aware")
        if self.last_run_at is not None:
            elapsed = (current_time - self.last_run_at).total_seconds()
            if elapsed < self.interval_seconds:
                return None
        action = self.runtime.observe_live_features(reference, current)
        self.last_run_at = current_time
        if action.status == "NORMAL":
            self._last_alert_fingerprint = None
        else:
            fingerprint = (
                action.status,
                tuple(action.report.drifted_features),
                round(action.report.max_psi, 6),
            )
            if fingerprint != self._last_alert_fingerprint:
                delivered = await self._notify(action)
                if delivered:
                    self._last_alert_fingerprint = fingerprint
        return action

    async def run_forever(self, stop_event, batch_provider) -> None:
        """Run on an application-owned asyncio task until stop_event is set.

        batch_provider may be synchronous or async and returns (reference,
        current) feature mappings. The callback receives each changed warning
        or critical drift state. Shutdown is cooperative via asyncio.Event.
        """
        import asyncio
        import inspect
        from datetime import datetime, timezone

        while not stop_event.is_set():
            try:
                is_async_provider = inspect.iscoroutinefunction(
                    batch_provider
                ) or inspect.iscoroutinefunction(
                    getattr(batch_provider, "__call__", None)
                )
                if is_async_provider:
                    batches = await batch_provider()
                else:
                    batches = await asyncio.to_thread(batch_provider)
                if inspect.isawaitable(batches):
                    batches = await batches
                reference, current = batches
                await self.run_once(reference, current, now=datetime.now(timezone.utc))
            except Exception as exc:
                # Preserve a fail-closed governance state instead of leaving a
                # previous NORMAL report active when fresh data is unavailable.
                action = self.runtime.mark_drift_unavailable(
                    f"drift_provider_unavailable:{type(exc).__name__}"
                )
                fingerprint = ("UNAVAILABLE", action.reasons)
                if fingerprint != self._last_alert_fingerprint:
                    delivered = await self._notify(action)
                    if delivered:
                        self._last_alert_fingerprint = fingerprint
                logger.exception(
                    "statistical drift check unavailable; promotion remains blocked"
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue
