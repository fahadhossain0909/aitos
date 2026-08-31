"""Trade domain models — Opportunity (input to the lifecycle) and Trade
(the lifecycle's own record), mirroring the ``trades`` table in spec
section 7.2 plus the state machine in section 30.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class TradeSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class TradeLifecycleState(str, Enum):
    """Spec section 30.1's state machine, minus the post-close journal/review/
    learning stages (those belong to the Journal System, a later phase)."""

    OPPORTUNITY_DETECTED = "opportunity_detected"
    ENTRY_VALIDATED = "entry_validated"
    REJECTED = "rejected"  # risk veto / hard limit / governance denial
    ORDER_SUBMITTED = "order_submitted"
    POSITION_OPENED = "position_opened"
    EXIT_TRIGGERED = "exit_triggered"
    POSITION_CLOSED = "position_closed"


def _new_trade_id() -> str:
    return f"trade-{uuid.uuid4().hex[:12]}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Public aliases — used by other modules (e.g. aitos.trading.lifecycle).
new_trade_id = _new_trade_id
utc_now_iso = _utc_now_iso


@dataclass(frozen=True)
class Opportunity:
    """What a scanner/agent hands the Trade Lifecycle to consider entering.

    Stop loss / take-profit levels are expected to already be computed by
    upstream intelligence (Smart Entry / Smart SL / Smart TP, spec §30.2 —
    the Opportunity Scanner phase). This module focuses on lifecycle
    orchestration, not on generating these levels itself.
    """

    symbol: str
    side: TradeSide
    entry_price: float
    stop_loss_price: float
    take_profit_levels: list[float]  # ordered, nearest first
    confidence: float
    strategy_id: str
    rationale: str
    agent_consensus: dict[str, Any] = field(default_factory=dict)
    is_production: bool = False
    approved_by: str | None = None
    trailing_sl_enabled: bool = False
    breakeven_at_r_multiple: float | None = (
        1.0  # move SL to entry after 1R profit by default
    )
    regime: str = (
        "unknown"  # market regime at scan time (trending/ranging/volatile) — feeds RL/KG consumers
    )
    opportunity_id: str = field(default_factory=lambda: f"opp-{uuid.uuid4().hex[:12]}")
    detected_at: str = field(default_factory=_utc_now_iso)


@dataclass
class PartialExit:
    price: float
    size_usd: float
    r_multiple: float
    at: str = field(default_factory=_utc_now_iso)


@dataclass
class Trade:
    """Mutable lifecycle record for one trade, from validation through close."""

    trade_id: str
    symbol: str
    side: TradeSide
    entry_price: float
    quantity: float
    leverage: float
    position_size_usd: float
    risk_amount_usd: float
    strategy_id: str
    agent_consensus: dict[str, Any]
    explanation: str
    sl_price: float
    tp_price: float
    state: TradeLifecycleState
    entry_time: str
    trailing_sl_enabled: bool = False
    breakeven_triggered: bool = False
    breakeven_at_r_multiple: float | None = None
    take_profit_levels: list[float] = field(default_factory=list)
    partial_exits: list[PartialExit] = field(default_factory=list)
    sl_order_id: str | None = None
    tp_order_ids: list[str] = field(default_factory=list)
    regime: str = "unknown"
    exit_price: float | None = None
    exit_time: str | None = None
    exit_reason: str | None = None
    pnl: float | None = None
    pnl_percent: float | None = None
    rejection_reason: str | None = None
    # Immutable initial risk distance. MAE/MFE and R multiples must use the
    # original entry-to-initial-SL distance, not a later breakeven/trailing SL.
    initial_r_distance: float | None = None
    # Excursion telemetry. Values are absolute price excursions from entry;
    # they are populated by the lifecycle's market-price update path.
    mae_price: float | None = None
    mfe_price: float | None = None
    mae_r: float | None = None
    mfe_r: float | None = None
    updated_at: str = field(default_factory=_utc_now_iso)

    def __post_init__(self) -> None:
        if self.initial_r_distance is None:
            object.__setattr__(
                self, "initial_r_distance", abs(self.entry_price - self.sl_price)
            )

    @property
    def r_distance(self) -> float:
        """Return the immutable 1R distance defined at trade entry."""
        return self.initial_r_distance or 0.0

    def unrealized_r_multiple(self, current_price: float) -> float:
        if self.r_distance == 0:
            return 0.0
        direction = 1 if self.side == TradeSide.LONG else -1
        return ((current_price - self.entry_price) * direction) / self.r_distance

    def record_excursion(self, current_price: float) -> None:
        """Update MAE/MFE telemetry from a valid market price observation."""
        if current_price <= 0:
            raise ValueError("current_price must be positive")
        direction = 1 if self.side == TradeSide.LONG else -1
        signed_move = (current_price - self.entry_price) * direction
        adverse = max(0.0, -signed_move)
        favorable = max(0.0, signed_move)
        self.mae_price = max(self.mae_price or 0.0, adverse)
        self.mfe_price = max(self.mfe_price or 0.0, favorable)
        if self.r_distance > 0:
            self.mae_r = self.mae_price / self.r_distance
            self.mfe_r = self.mfe_price / self.r_distance

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "leverage": self.leverage,
            "position_size_usd": self.position_size_usd,
            "risk_amount_usd": self.risk_amount_usd,
            "strategy_id": self.strategy_id,
            "agent_consensus": self.agent_consensus,
            "explanation": self.explanation,
            "sl_price": self.sl_price,
            "tp_price": self.tp_price,
            "take_profit_levels": list(self.take_profit_levels),
            "state": self.state.value,
            "entry_time": self.entry_time,
            "exit_price": self.exit_price,
            "exit_time": self.exit_time,
            "exit_reason": self.exit_reason,
            "pnl": self.pnl,
            "pnl_percent": self.pnl_percent,
            "rejection_reason": self.rejection_reason,
            "trailing_sl_enabled": self.trailing_sl_enabled,
            "sl_order_id": self.sl_order_id,
            "tp_order_ids": list(self.tp_order_ids),
            "regime": self.regime,
            "partial_exits": [pe.__dict__ for pe in self.partial_exits],
            "initial_r_distance": self.initial_r_distance,
            "mae_price": self.mae_price,
            "mfe_price": self.mfe_price,
            "mae_r": self.mae_r,
            "mfe_r": self.mfe_r,
        }
