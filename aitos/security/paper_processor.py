"""Paper-only order-intent processor skeleton.

This module does not expose HTTP, load credentials, create signatures, or connect
to an exchange. Its only adapter is an in-process deterministic paper adapter.
It exists to exercise validation, durable replay reservation and crash behavior
before a separately reviewed remote signer is designed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from aitos.security.replay_store import (
    IntentReservation,
    ReplayStoreError,
    SQLiteReplayStore,
)
from aitos.security.risk_limits import AccountEvidenceProvider, SignerRiskLimits
from aitos.security.signing_policy import (
    OrderIntentPolicy,
    ValidatedOrderIntent,
    parse_order_intent_json,
    validate_order_intent,
)


@dataclass(frozen=True)
class PaperExecutionReceipt:
    status: str
    outcome_code: str


@dataclass(frozen=True)
class PaperIntentResult:
    intent_id: str
    status: str
    outcome_code: str | None
    duplicate: bool


class PaperExecutionAdapter:
    """Deterministic in-process adapter; never performs network or venue I/O."""

    paper_only = True

    def execute(self, intent: ValidatedOrderIntent) -> PaperExecutionReceipt:
        return PaperExecutionReceipt(
            status="accepted",
            outcome_code="paper_order_recorded",
        )


class PaperOnlyIntentProcessor:
    """Validate, reserve once, then execute through the paper-only adapter."""

    def __init__(
        self,
        *,
        policy: OrderIntentPolicy,
        replay_store: SQLiteReplayStore,
        adapter: PaperExecutionAdapter | None = None,
        risk_limits: SignerRiskLimits | None = None,
        account_evidence_provider: AccountEvidenceProvider | None = None,
    ) -> None:
        if policy.allow_market_orders or "MARKET" in policy.allowed_order_types:
            raise ValueError("paper_skeleton_supports_limit_orders_only")
        self.policy = policy
        self.replay_store = replay_store
        self.adapter = adapter or PaperExecutionAdapter()
        if risk_limits is not None and not isinstance(risk_limits, SignerRiskLimits):
            raise TypeError("signer_risk_limits_required")
        self.risk_limits = risk_limits
        if (risk_limits is None) != (account_evidence_provider is None):
            raise ValueError("risk_limits_and_evidence_provider_must_be_paired")
        self.account_evidence_provider = account_evidence_provider
        if not getattr(self.adapter, "paper_only", False):
            raise ValueError("non_paper_adapter_forbidden")

    def process(
        self,
        payload: str | bytes | Mapping[str, Any],
        *,
        now_ms: int,
    ) -> PaperIntentResult:
        parsed = (
            parse_order_intent_json(payload)
            if isinstance(payload, (str, bytes))
            else payload
        )
        intent = validate_order_intent(
            parsed,
            policy=self.policy,
            now_ms=now_ms,
        )
        # Independent signer-side preflight occurs before consuming an intent nonce.
        # The caller must obtain this evidence from a separately authenticated source.
        if self.risk_limits is not None:
            # Evidence is fetched inside the trusted signer boundary, never supplied
            # as an untrusted field by the remote VPS order-intent request.
            snapshot = self.account_evidence_provider.get_snapshot(
                intent.account_alias, now_ms=now_ms
            )
            self.risk_limits.authorize(intent, snapshot, now_ms=now_ms)
        reservation = self.replay_store.reserve(intent, now_ms=now_ms)
        if not reservation.created:
            return self._duplicate_result(reservation)

        self.replay_store.mark_dispatching(intent.intent_id, now_ms=now_ms)
        try:
            receipt = self.adapter.execute(intent)
            if receipt.status not in {"accepted", "rejected", "unknown"}:
                raise ReplayStoreError("paper_adapter_returned_invalid_status")
            self.replay_store.finish(
                intent.intent_id,
                status=receipt.status,
                outcome_code=receipt.outcome_code,
                now_ms=now_ms,
            )
            return PaperIntentResult(
                intent_id=intent.intent_id,
                status=receipt.status,
                outcome_code=receipt.outcome_code,
                duplicate=False,
            )
        except Exception:
            # Once execution may have begun, the only safe response is UNKNOWN.
            # A retry must reconcile this intent; it must not call execute again.
            try:
                self.replay_store.finish(
                    intent.intent_id,
                    status="unknown",
                    outcome_code="paper_dispatch_ambiguous",
                    now_ms=now_ms,
                )
            except ReplayStoreError:
                # A failed durable transition is itself fail-closed. The durable
                # DISPATCHING row prevents a duplicate from being re-executed.
                pass
            return PaperIntentResult(
                intent_id=intent.intent_id,
                status="unknown",
                outcome_code="paper_dispatch_ambiguous",
                duplicate=False,
            )

    @staticmethod
    def _duplicate_result(reservation: IntentReservation) -> PaperIntentResult:
        return PaperIntentResult(
            intent_id=reservation.intent_id,
            status=reservation.status,
            outcome_code=reservation.outcome_code,
            duplicate=True,
        )
