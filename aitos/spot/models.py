"""Market-neutral, Decimal-safe Binance Spot domain contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

from aitos.market_data.venues import InstrumentKey, MarketType, Venue


class SpotOrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class SpotOrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class SpotOrderStatus(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SpotInstrument:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    permissions: tuple[str, ...] = ()
    is_spot_trading_allowed: bool = False

    @property
    def instrument_key(self) -> InstrumentKey:
        return InstrumentKey(Venue.BINANCE, MarketType.SPOT, self.symbol)

    def __post_init__(self) -> None:
        for name in ("symbol", "base_asset", "quote_asset", "status"):
            if not getattr(self, name):
                raise ValueError(f"{name} is required")
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "base_asset", self.base_asset.upper())
        object.__setattr__(self, "quote_asset", self.quote_asset.upper())


@dataclass(frozen=True, slots=True)
class SpotBalance:
    asset: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass(frozen=True, slots=True)
class SpotOrderRequest:
    symbol: str
    side: SpotOrderSide
    order_type: SpotOrderType
    quantity: Decimal | None = None
    quote_order_quantity: Decimal | None = None
    price: Decimal | None = None
    time_in_force: str | None = None
    client_order_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        if (self.quantity is None) == (self.quote_order_quantity is None):
            raise ValueError("provide exactly one of quantity or quote_order_quantity")
        if self.quantity is not None and self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.quote_order_quantity is not None and self.quote_order_quantity <= 0:
            raise ValueError("quote_order_quantity must be positive")
        if self.order_type is SpotOrderType.LIMIT:
            if self.price is None or self.price <= 0:
                raise ValueError("LIMIT orders require a positive price")
            if self.time_in_force not in {"GTC", "IOC", "FOK"}:
                raise ValueError("LIMIT orders require GTC, IOC or FOK")
        elif self.price is not None or self.time_in_force is not None:
            raise ValueError("MARKET orders cannot specify price or time_in_force")


@dataclass(frozen=True, slots=True)
class SpotFill:
    price: Decimal
    quantity: Decimal
    commission: Decimal
    commission_asset: str
    trade_id: int | None = None

    @property
    def quote_quantity(self) -> Decimal:
        return self.price * self.quantity


@dataclass(frozen=True, slots=True)
class SpotOrder:
    symbol: str
    order_id: int
    client_order_id: str
    side: SpotOrderSide
    status: SpotOrderStatus
    executed_quantity: Decimal
    cumulative_quote_quantity: Decimal
    fills: tuple[SpotFill, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def average_price(self) -> Decimal | None:
        if self.executed_quantity <= 0:
            return None
        return self.cumulative_quote_quantity / self.executed_quantity
