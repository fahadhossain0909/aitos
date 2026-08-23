"""Domain models for the Risk Engine (spec section 31)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, model_validator


class RiskAction(str, Enum):
    NORMAL = "normal"
    REDUCE_SIZE = "reduce_size"
    NO_NEW_ENTRIES = "no_new_entries"
    EMERGENCY_STOP = "emergency_stop"


class CircuitBreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class RiskLimits(BaseModel):
    max_risk_per_trade_pct: float = Field(default=1.0, gt=0)
    max_risk_per_trade_hard_cap_pct: float = Field(default=2.0, gt=0)
    max_risk_per_day_pct: float = Field(default=3.0, gt=0)
    max_risk_per_day_hard_cap_pct: float = Field(default=5.0, gt=0)
    max_risk_per_week_pct: float = Field(default=5.0, gt=0)
    max_risk_per_week_hard_cap_pct: float = Field(default=10.0, gt=0)
    max_drawdown_pct: float = Field(default=10.0, gt=0)
    max_drawdown_hard_cap_pct: float = Field(default=20.0, gt=0)
    max_leverage: float = Field(default=10.0, ge=1)
    max_leverage_hard_cap: float = Field(default=125.0, ge=1)
    max_correlated_exposure_pct: float = Field(default=15.0, gt=0)
    max_correlated_exposure_hard_cap_pct: float = Field(default=25.0, gt=0)
    max_sector_exposure_pct: float = Field(default=20.0, gt=0)
    max_sector_exposure_hard_cap_pct: float = Field(default=40.0, gt=0)
    max_open_positions: int = Field(default=10, ge=1)
    max_open_positions_hard_cap: int = Field(default=20, ge=1)
    min_data_freshness_seconds: float = Field(default=5.0, gt=0)
    min_data_freshness_hard_cap_seconds: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _defaults_within_hard_caps(self) -> "RiskLimits":
        pairs = [
            ("max_risk_per_trade_pct", "max_risk_per_trade_hard_cap_pct"),
            ("max_risk_per_day_pct", "max_risk_per_day_hard_cap_pct"),
            ("max_risk_per_week_pct", "max_risk_per_week_hard_cap_pct"),
            ("max_drawdown_pct", "max_drawdown_hard_cap_pct"),
            ("max_leverage", "max_leverage_hard_cap"),
            ("max_correlated_exposure_pct", "max_correlated_exposure_hard_cap_pct"),
            ("max_sector_exposure_pct", "max_sector_exposure_hard_cap_pct"),
            ("max_open_positions", "max_open_positions_hard_cap"),
        ]
        for default_field, cap_field in pairs:
            if getattr(self, default_field) > getattr(self, cap_field):
                raise ValueError(f"{default_field} cannot exceed {cap_field}")
        if self.min_data_freshness_seconds > self.min_data_freshness_hard_cap_seconds:
            raise ValueError("min_data_freshness_seconds cannot exceed its hard cap")
        return self


@dataclass(frozen=True)
class PositionExposure:
    symbol: str
    notional_usd: float
    leverage: float
    sector: str = ""

    def __post_init__(self) -> None:
        if not self.sector or self.sector == "unclassified":
            from aitos.risk.sector import sector_for_symbol

            object.__setattr__(self, "sector", sector_for_symbol(self.symbol))


@dataclass(frozen=True)
class PortfolioState:
    equity_usd: float
    peak_equity_usd: float
    positions: Tuple[PositionExposure, ...] = ()
    daily_pnl_pct: float = 0.0
    weekly_pnl_pct: float = 0.0
    volatility_percentile: float = 50.0
    regime: str = "normal"
    max_pairwise_correlation: float = 0.0
    api_error_rate_pct: float = 0.0
    api_latency_ms: float = 0.0
    data_freshness_seconds: float = 0.0
    model_accuracy: float = 0.75
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def current_drawdown_pct(self) -> float:
        if self.peak_equity_usd <= 0:
            return 0.0
        return max(
            0.0, (self.peak_equity_usd - self.equity_usd) / self.peak_equity_usd * 100
        )

    @property
    def gross_exposure_usd(self) -> float:
        return sum(p.notional_usd for p in self.positions)

    @property
    def max_position_leverage(self) -> float:
        return max((p.leverage for p in self.positions), default=0.0)

    @property
    def sector_exposure_pct(self) -> Dict[str, float]:
        if self.equity_usd <= 0:
            return {}
        totals: Dict[str, float] = {}
        for p in self.positions:
            sector = p.sector or "other"
            totals[sector] = totals.get(sector, 0.0) + p.notional_usd
        return {
            sector: notional / self.equity_usd * 100
            for sector, notional in totals.items()
        }


@dataclass(frozen=True)
class RiskScoreBreakdown:
    position_risk: float
    market_risk: float
    system_risk: float
    portfolio_risk: float
    total: float
    action: RiskAction
    explanation: List[str] = field(default_factory=list)
    computed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "position_risk": self.position_risk,
            "market_risk": self.market_risk,
            "system_risk": self.system_risk,
            "portfolio_risk": self.portfolio_risk,
            "total": self.total,
            "action": self.action.value,
            "explanation": self.explanation,
            "computed_at": self.computed_at,
        }


@dataclass(frozen=True)
class LimitBreach:
    limit_name: str
    limit_value: float
    observed_value: float
    is_hard_cap: bool
    message: str


@dataclass(frozen=True)
class PositionSizeResult:
    position_size_usd: float
    leverage: float
    risk_amount_usd: float
    rationale: str
