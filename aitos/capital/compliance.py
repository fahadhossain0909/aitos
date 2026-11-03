"""Pre-trade prop-firm compliance firewall."""

from __future__ import annotations

from dataclasses import dataclass

from aitos.capital.models import AccountSnapshot, PropFirmProfile
from aitos.execution.order_executor import OrderRequest


@dataclass(frozen=True)
class ComplianceDecision:
    allowed: bool
    reason: str = ""


class PropComplianceEngine:
    """Fail-closed local guard for account-specific prop rules."""

    def evaluate(
        self,
        profile: PropFirmProfile,
        account: AccountSnapshot,
        request: OrderRequest,
        projected_loss: float = 0.0,
    ) -> ComplianceDecision:
        rules = profile.rules
        if not rules.api_allowed:
            return ComplianceDecision(False, "prop profile does not allow API trading")
        if not rules.automation_allowed:
            return ComplianceDecision(
                False, "prop profile does not allow automated trading"
            )
        if (
            rules.daily_loss_limit is not None
            and account.daily_loss + max(0.0, projected_loss) >= rules.daily_loss_limit
        ):
            return ComplianceDecision(False, "daily loss limit would be breached")
        if (
            rules.max_drawdown is not None
            and account.drawdown + max(0.0, projected_loss) >= rules.max_drawdown
        ):
            return ComplianceDecision(False, "maximum drawdown would be breached")
        notional = abs(request.quantity * request.reference_price)
        if rules.max_order_notional is not None and notional > rules.max_order_notional:
            return ComplianceDecision(
                False, "order notional exceeds prop profile limit"
            )
        return ComplianceDecision(True, "compliant")
