"""Provider-neutral models for personal, exchange, and prop capital."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AccountMode(str, Enum):
    PERSONAL = "personal"
    SIMULATED_FUNDED = "simulated_funded"
    LIVE_FUNDED = "live_funded"
    EVALUATION = "evaluation"


@dataclass(frozen=True)
class PropRuleSet:
    """Rules enforced locally before an order reaches a venue."""

    daily_loss_limit: float | None = None
    max_drawdown: float | None = None
    daily_loss_uses_equity: bool = True
    max_drawdown_uses_equity: bool = True
    api_allowed: bool = True
    automation_allowed: bool = True
    news_trading_allowed: bool = True
    overnight_allowed: bool = True
    weekend_allowed: bool = True
    max_order_notional: float | None = None


@dataclass(frozen=True)
class PropFirmProfile:
    provider: str
    platform: str
    rules: PropRuleSet
    account_mode: AccountMode = AccountMode.EVALUATION
    live_api: bool = False
    documentation_url: str | None = None


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    provider: str
    equity: float
    balance: float
    day_start_equity: float
    high_water_mark: float
    unrealized_pnl: float = 0.0
    realized_pnl_today: float = 0.0

    @property
    def daily_loss(self) -> float:
        return max(0.0, self.day_start_equity - self.equity)

    @property
    def drawdown(self) -> float:
        return max(0.0, self.high_water_mark - self.equity)
