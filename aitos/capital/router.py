"""Capital-aware execution routing."""

from __future__ import annotations

from dataclasses import dataclass

from aitos.capital.compliance import PropComplianceEngine
from aitos.capital.models import AccountSnapshot, PropFirmProfile
from aitos.execution.order_executor import OrderExecutor, OrderRequest, OrderResult
from aitos.logging_setup import get_logger

logger = get_logger("aitos.capital.router")


@dataclass(frozen=True)
class VenueQuote:
    venue: str
    account_id: str
    available_risk: float
    expected_cost_bps: float = 0.0
    execution_score: float = 1.0


class CapitalRouter:
    """Select a compliant capital venue without coupling strategy to vendors."""

    def __init__(self, compliance: PropComplianceEngine | None = None) -> None:
        self.compliance = compliance or PropComplianceEngine()
        self._venues: dict[
            str, tuple[OrderExecutor, PropFirmProfile, AccountSnapshot]
        ] = {}

    @property
    def has_venues(self) -> bool:
        return bool(self._venues)

    def register(
        self,
        venue: str,
        executor: OrderExecutor,
        profile: PropFirmProfile,
        account: AccountSnapshot,
    ) -> None:
        if not venue.strip():
            raise ValueError("venue name must not be empty")
        if account.provider != profile.provider:
            raise ValueError("profile and account providers must match")
        self._venues[venue] = (executor, profile, account)

    @property
    def supports_exchange_side_stops(self) -> bool:
        # A routed executor cannot safely promise stop capability merely
        # because one venue supports it: the selected venue is determined per
        # order. Keep the capability conservative until protection routing is
        # implemented explicitly.
        return False

    async def get_account_balance(self, asset: str = "USDT") -> float:
        """Sum balances across every registered venue that can report one.

        ``OrderExecutor`` doesn't declare ``get_account_balance`` as part of
        its abstract contract (not every executor is bank-account-queryable
        -- e.g. paper/simulated executors), so venues whose executor doesn't
        implement it are skipped rather than raising. One venue's balance
        call failing also doesn't abort the rest -- a single broker outage
        shouldn't make the whole portfolio's equity unreadable.
        """
        total = 0.0
        for venue, (executor, _profile, _account) in self._venues.items():
            get_balance = getattr(executor, "get_account_balance", None)
            if get_balance is None:
                continue
            try:
                total += float(await get_balance(asset))
            except Exception:
                logger.exception(
                    "capital router balance fetch failed",
                    extra={"aitos_extra": {"venue": venue, "asset": asset}},
                )
        return total

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
        return sorted(
            result, key=lambda q: (-q.available_risk, q.expected_cost_bps, q.venue)
        )

    async def submit_best(
        self, request: OrderRequest, projected_loss: float = 0.0
    ) -> tuple[str, OrderResult]:
        candidates = self.candidates(request, projected_loss)
        if not candidates:
            raise RuntimeError("no compliant capital venue is available")
        for candidate in candidates:
            executor, profile, account = self._venues[candidate.venue]
            decision = self.compliance.evaluate(
                profile, account, request, projected_loss
            )
            if not decision.allowed:
                continue
            try:
                return candidate.venue, await executor.submit_order(request)
            except Exception:
                # Try the next compliant capital venue. This makes routing
                # resilient to a single venue outage without bypassing rules.
                continue
        raise RuntimeError("all compliant capital venues failed execution")
