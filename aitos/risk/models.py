"""Domain models for the Risk Engine (spec section 31)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
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
    side: str | None = None
    risk_amount_usd: float | None = None

    def __post_init__(self) -> None:
        if self.side is not None:
            normalized_side = str(self.side).upper()
            if normalized_side not in {"LONG", "SHORT"}:
                raise ValueError("position side must be LONG or SHORT")
            object.__setattr__(self, "side", normalized_side)
        if self.risk_amount_usd is not None and (
            not isfinite(float(self.risk_amount_usd)) or self.risk_amount_usd < 0
        ):
            raise ValueError("risk_amount_usd must be finite and non-negative")
        if not self.sector or self.sector == "unclassified":
            from aitos.risk.sector import sector_for_symbol

            object.__setattr__(self, "sector", sector_for_symbol(self.symbol))


@dataclass(frozen=True)
class PendingOrderExposure:
    """Unfilled entry exposure projected into portfolio risk before dispatch."""

    order_id: str
    symbol: str
    side: str
    remaining_notional_usd: float
    risk_amount_usd: float
    leverage: float = 1.0

    def __post_init__(self) -> None:
        order_id = str(self.order_id).strip()
        symbol = str(self.symbol).strip().upper()
        if not order_id or not symbol:
            raise ValueError("pending order identity and symbol are required")
        normalized_side = str(self.side).strip().upper()
        if normalized_side not in {"LONG", "SHORT"}:
            raise ValueError("pending order side must be LONG or SHORT")
        object.__setattr__(self, "order_id", order_id)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", normalized_side)
        for name in ("remaining_notional_usd", "risk_amount_usd"):
            value = float(getattr(self, name))
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if not isfinite(float(self.leverage)) or self.leverage <= 0:
            raise ValueError("pending order leverage must be finite and positive")


@dataclass(frozen=True)
class PortfolioState:
    equity_usd: float
    peak_equity_usd: float
    positions: tuple[PositionExposure, ...] = ()
    daily_pnl_pct: float = 0.0
    weekly_pnl_pct: float = 0.0
    consecutive_losses: int = 0
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
    pending_orders: tuple[PendingOrderExposure, ...] = ()

    def __post_init__(self) -> None:
        order_ids = [order.order_id for order in self.pending_orders]
        if len(order_ids) != len(set(order_ids)):
            raise ValueError("pending order IDs must be unique in a portfolio snapshot")

    @property
    def projected_open_position_count(self) -> int:
        """Count unique instruments across open positions and pending orders."""
        symbols = {
            str(position.symbol).strip().upper()
            for position in self.positions
            if str(position.symbol).strip()
        }
        symbols.update(order.symbol for order in self.pending_orders if order.symbol)
        return len(symbols)

    @property
    def current_drawdown_pct(self) -> float:
        if self.peak_equity_usd <= 0:
            return 0.0
        return max(
            0.0, (self.peak_equity_usd - self.equity_usd) / self.peak_equity_usd * 100
        )

    @property
    def gross_exposure_usd(self) -> float:
        return sum(p.notional_usd for p in self.positions) + sum(
            order.remaining_notional_usd for order in self.pending_orders
        )

    @property
    def max_position_leverage(self) -> float:
        return max(
            [p.leverage for p in self.positions]
            + [order.leverage for order in self.pending_orders],
            default=0.0,
        )

    @property
    def sector_exposure_pct(self) -> dict[str, float]:
        if self.equity_usd <= 0:
            return {}
        totals: dict[str, float] = {}
        for p in self.positions:
            sector = p.sector or "other"
            totals[sector] = totals.get(sector, 0.0) + p.notional_usd
        from aitos.risk.sector import sector_for_symbol

        for order in self.pending_orders:
            sector = sector_for_symbol(order.symbol)
            totals[sector] = totals.get(sector, 0.0) + order.remaining_notional_usd
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
    explanation: list[str] = field(default_factory=list)
    computed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
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
