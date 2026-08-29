"""Order execution abstraction.

Live order placement against Binance (or any venue) is intentionally out
of scope for this module — per the AI Constitution, any production
trading action must clear ``AIKernel.enforce_governance`` first, and a
live executor is its own security-sensitive piece of work (API key
handling, order validation, retry/idempotency semantics) that deserves a
dedicated phase rather than being bolted onto the lifecycle state machine.

``SimulatedOrderExecutor`` (paper trading / sandbox agents, spec's
"sandbox only" weight tier) fills instantly at the requested price so the
rest of the Trade Lifecycle can be built and tested against a real
interface today.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

from aitos.models.trade import TradeSide


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: TradeSide
    quantity: float
    reference_price: float
    order_type: str = "MARKET"
    client_order_id: str | None = None


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    symbol: str
    side: TradeSide
    filled_quantity: float
    fill_price: float
    filled_at: str = field(default_factory=_utc_now_iso)
    success: bool = True
    error: str | None = None


class OrderExecutor(ABC):
    @abstractmethod
    async def submit_order(self, request: OrderRequest) -> OrderResult: ...

    @property
    def supports_exchange_side_stops(self) -> bool:
        """Whether this executor can place resting stop/take-profit orders
        on the exchange itself (vs. the Trade Lifecycle only monitoring
        virtually via ``update_price``). ``False`` by default — override in
        executors that actually support it (e.g. ``BinanceFuturesOrderExecutor``)."""
        return False

    async def place_stop_loss_order(
        self, symbol: str, side: TradeSide, quantity: float, stop_price: float
    ) -> OrderResult:
        raise NotImplementedError(
            f"{type(self).__name__} does not support exchange-side stop orders"
        )

    async def place_take_profit_order(
        self, symbol: str, side: TradeSide, quantity: float, take_profit_price: float
    ) -> OrderResult:
        raise NotImplementedError(
            f"{type(self).__name__} does not support exchange-side take-profit orders"
        )

    async def cancel_resting_order(self, symbol: str, order_id: str) -> None:
        raise NotImplementedError(
            f"{type(self).__name__} does not support cancelling resting orders"
        )

    async def get_resting_order_status(
        self, symbol: str, order_id: str
    ) -> str | None:
        """Return the exchange's status string for a resting order (e.g.
        'FILLED', 'NEW', 'CANCELED'), or ``None`` if not supported."""
        return None


class SimulatedOrderExecutor(OrderExecutor):
    """Paper-trading executor: fills the full quantity instantly at
    ``reference_price`` (optionally with a fixed slippage assumption)."""

    def __init__(self, slippage_bps: float = 0.0) -> None:
        self._slippage_bps = slippage_bps
        self._order_counter = 0

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        self._order_counter += 1
        slippage_factor = 1 + (self._slippage_bps / 10_000) * (
            1 if request.side == TradeSide.LONG else -1
        )
        fill_price = request.reference_price * slippage_factor
        return OrderResult(
            order_id=f"sim-{self._order_counter}",
            symbol=request.symbol,
            side=request.side,
            filled_quantity=request.quantity,
            fill_price=round(fill_price, 8),
        )
