"""Capital-aware execution routing."""

from __future__ import annotations

from dataclasses import dataclass

from aitos.capital.compliance import PropComplianceEngine
from aitos.capital.models import AccountSnapshot, PropFirmProfile
from aitos.execution.order_executor import OrderExecutor, OrderRequest, OrderResult


@dataclass(frozen=True)
class VenueQuote:
    venue: str
    account_id: str
    available_risk: float
    expected_cost_bps: float = 0.0
    execution_score: float = 1.0


class CapitalRouter:
    """Selects a compliant capital venue without coupling strategy to vendors."""

    def __init__(self, compliance: PropComplianceEngine | None = None) -> None:
        self.compliance = compliance or PropComplianceEngine()
        self._venues: dict[
            str, tuple[OrderExecutor, PropFirmProfile, AccountSnapshot]
        ] = {}

    def register(
        self,
        venue: str,
        executor: OrderExecutor,
        profile: PropFirmProfile,
        account: AccountSnapshot,
    ) -> None:
        self._venues[venue] = (executor, profile, account)

    def candidates(
        self, request: OrderRequest, projected_loss: float = 0.0
    ) -> list[VenueQuote]:
        result: list[VenueQuote] = []
        for venue, (_, profile, account) in self._venues.items():
            decision = self.compliance.evaluate(
                profile, account, request, projected_loss
            )
            if decision.allowed:
                remaining_daily = (
                    float("inf")
                    if profile.rules.daily_loss_limit is None
                    else max(0.0, profile.rules.daily_loss_limit - account.daily_loss)
                )
                result.append(VenueQuote(venue, account.account_id, remaining_daily))
        return sorted(result, key=lambda q: (-q.available_risk, q.expected_cost_bps))

    async def submit_best(
        self,
        request: OrderRequest,
        projected_loss: float = 0.0,
    ) -> tuple[str, OrderResult]:
        candidates = self.candidates(request, projected_loss)
        if not candidates:
            raise RuntimeError("no compliant capital venue is available")
        venue = candidates[0].venue
        executor, profile, account = self._venues[venue]
        decision = self.compliance.evaluate(profile, account, request, projected_loss)
        if not decision.allowed:
            raise RuntimeError(decision.reason)
        return venue, await executor.submit_order(request)
