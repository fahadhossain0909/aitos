"""Provider/venue adapter protocols for future multi-asset execution."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from .contracts import Instrument, MarketEvent


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    instrument: Instrument
    side: str
    target_quantity: float
    max_slippage_bps: float = 25.0
    urgency: str = "normal"
    reduce_only: bool = False
    reason: str = "strategy"

    def __post_init__(self) -> None:
        if self.side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if self.target_quantity <= 0:
            raise ValueError("target_quantity must be positive")
        if self.max_slippage_bps < 0:
            raise ValueError("max_slippage_bps must be non-negative")


class MarketDataAdapter(Protocol):
    """Adapter contract for crypto, FX, equities, rates, futures and commodities."""

    async def events(self) -> AsyncIterator[MarketEvent]: ...

    async def instruments(self) -> list[Instrument]: ...


class ExecutionAdapter(Protocol):
    """Venue/broker boundary. Strategies must never call vendor APIs directly."""

    async def submit(self, intent: ExecutionIntent) -> str: ...

    async def cancel(self, order_id: str) -> None: ...

    async def positions(self) -> list[tuple[Instrument, float]]: ...
