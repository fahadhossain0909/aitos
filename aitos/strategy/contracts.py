"""Strategy-layer contracts shared by all AITOS strategy families."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence


class StrategyFamily(str, Enum):
    DIRECTIONAL = "directional"
    ARBITRAGE = "arbitrage"
    FUNDING_BASIS = "funding_basis"
    MARKET_MAKING = "market_making"
    HEDGING = "hedging"
    OPTIONS = "options"
    REGIME = "regime"
    SPECIAL = "special"


class StrategyMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"
    SHADOW = "shadow"


@dataclass(frozen=True)
class ExecutionIntent:
    """Venue-neutral desired action; never contains broker API payloads."""

    instrument_id: str
    side: str
    quantity: float
    order_type: str = "market"
    limit_price: float | None = None
    reduce_only: bool = False
    hedge_group: str | None = None
    strategy_id: str = ""
    rationale: str = ""
    urgency: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.side not in {"buy", "sell"}:
            raise ValueError("side must be 'buy' or 'sell'")
        if not 0.0 <= self.urgency <= 1.0:
            raise ValueError("urgency must be between 0 and 1")


@dataclass(frozen=True)
class CapitalRequest:
    strategy_id: str
    requested_notional: float
    max_loss: float
    expected_edge: float
    confidence: float = 0.0
    priority: float = 0.0


@dataclass(frozen=True)
class MarketSnapshot:
    """Compact, strategy-facing market state. Providers may supply more data."""

    instrument_id: str
    mid: float
    spread_bps: float = 0.0
    volatility: float = 0.0
    funding_rate: float = 0.0
    basis_bps: float = 0.0
    imbalance: float = 0.0
    aggressive_buy_ratio: float = 0.5
    regime: str = "unknown"
    liquidity_score: float = 0.0
    features: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class PositionEffect:
    instrument_id: str
    target_delta: float
    hedge_group: str | None = None


@dataclass(frozen=True)
class StrategyContext:
    now_ns: int
    mode: StrategyMode
    snapshots: Mapping[str, MarketSnapshot]
    positions: Mapping[str, float] = field(default_factory=dict)
    available_capital: float = 0.0
    portfolio_delta: float = 0.0
    risk_budget: float = 0.0
    global_regime: str = "unknown"
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyResult:
    strategy_id: str
    family: StrategyFamily
    intents: Sequence[ExecutionIntent] = field(default_factory=tuple)
    capital_request: CapitalRequest | None = None
    position_effects: Sequence[PositionEffect] = field(default_factory=tuple)
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


class Strategy:
    """Minimal protocol-like base class for pluggable strategies."""

    strategy_id: str
    family: StrategyFamily

    def evaluate(self, context: StrategyContext) -> StrategyResult:
        raise NotImplementedError
