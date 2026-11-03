"""Journal domain models — spec §34, mirroring the ``journal_entries``
table (section 7.2) plus the Daily/Weekly/Monthly review structures §34.1
describes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class JournalEntryType(str, Enum):
    PRE_TRADE = "PRE_TRADE"
    POST_TRADE = "POST_TRADE"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    MISTAKE = "MISTAKE"
    LESSON = "LESSON"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JournalEntry:
    trade_id: str | None
    entry_type: JournalEntryType
    market_context: dict[str, Any]
    confidence_score: float | None = None
    order_flow_observations: dict[str, Any] = field(default_factory=dict)
    liquidity_observations: dict[str, Any] = field(default_factory=dict)
    amt_observations: dict[str, Any] = field(default_factory=dict)
    lead_lag_observations: dict[str, Any] = field(default_factory=dict)
    mistakes: list[str] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)
    entry_id: str = field(default_factory=lambda: f"journal-{uuid.uuid4().hex[:12]}")
    created_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "trade_id": self.trade_id,
            "entry_type": self.entry_type.value,
            "market_context": self.market_context,
            "confidence_score": self.confidence_score,
            "order_flow_observations": self.order_flow_observations,
            "liquidity_observations": self.liquidity_observations,
            "amt_observations": self.amt_observations,
            "lead_lag_observations": self.lead_lag_observations,
            "mistakes": self.mistakes,
            "lessons": self.lessons,
            "improvements": self.improvements,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class DailyReview:
    date: str
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    avg_r_multiple: float
    best_trade_pnl: float
    worst_trade_pnl: float

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class WeeklyReview:
    week_start: str
    total_trades: int
    total_pnl: float
    win_rate: float
    by_strategy: dict[str, dict[str, float]]  # strategy_id -> {trades, pnl, win_rate}

    def to_dict(self) -> dict[str, Any]:
        return {
            "week_start": self.week_start,
            "total_trades": self.total_trades,
            "total_pnl": self.total_pnl,
            "win_rate": self.win_rate,
            "by_strategy": self.by_strategy,
        }


@dataclass(frozen=True)
class MonthlyReview:
    month: str
    total_trades: int
    total_pnl: float
    sharpe_ratio: float
    max_drawdown_pct: float
    calmar_ratio: float

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()
