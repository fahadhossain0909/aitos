"""Domain models for the Risk Engine (spec section 31)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

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
    def _defaults_within_hard_caps(self) -> RiskLimits:
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
    positions: tuple[PositionExposure, ...] = ()
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "equity_usd": self.equity_usd,
            "peak_equity_usd": self.peak_equity_usd,
            "positions": [
                {
                    "symbol": position.symbol,
                    "notional_usd": position.notional_usd,
                    "leverage": position.leverage,
                    "sector": position.sector,
                }
                for position in self.positions
            ],
            "daily_pnl_pct": self.daily_pnl_pct,
            "weekly_pnl_pct": self.weekly_pnl_pct,
            "volatility_percentile": self.volatility_percentile,
            "regime": self.regime,
            "max_pairwise_correlation": self.max_pairwise_correlation,
            "api_error_rate_pct": self.api_error_rate_pct,
            "api_latency_ms": self.api_latency_ms,
            "data_freshness_seconds": self.data_freshness_seconds,
            "model_accuracy": self.model_accuracy,
            "timestamp": self.timestamp,
        }
