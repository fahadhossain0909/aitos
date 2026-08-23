"""Trade domain models — Opportunity (input to the lifecycle) and Trade
(the lifecycle's own record), mirroring the ``trades`` table in spec
section 7.2 plus the state machine in section 30.1.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


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
    take_profit_levels: List[float]  # ordered, nearest first
    confidence: float
    strategy_id: str
    rationale: str
    agent_consensus: Dict[str, Any] = field(default_factory=dict)
    is_production: bool = False
    approved_by: Optional[str] = None
    trailing_sl_enabled: bool = False
    breakeven_at_r_multiple: Optional[float] = (
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
    agent_consensus: Dict[str, Any]
    explanation: str
    sl_price: float
    tp_price: float
    state: TradeLifecycleState
    entry_time: str
    trailing_sl_enabled: bool = False
    breakeven_triggered: bool = False
    breakeven_at_r_multiple: Optional[float] = None
    take_profit_levels: List[float] = field(default_factory=list)
    partial_exits: List[PartialExit] = field(default_factory=list)
    sl_order_id: Optional[str] = None
    tp_order_ids: List[str] = field(default_factory=list)
    regime: str = "unknown"
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    exit_reason: Optional[str] = None
    pnl: Optional[float] = None
    pnl_percent: Optional[float] = None
    rejection_reason: Optional[str] = None
    updated_at: str = field(default_factory=_utc_now_iso)

    @property
    def r_distance(self) -> float:
        """Price distance representing 1R (initial risk unit)."""
        return abs(self.entry_price - self.sl_price)

    def unrealized_r_multiple(self, current_price: float) -> float:
        if self.r_distance == 0:
            return 0.0
        direction = 1 if self.side == TradeSide.LONG else -1
        return ((current_price - self.entry_price) * direction) / self.r_distance

    def to_dict(self) -> Dict[str, Any]:
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
        }
