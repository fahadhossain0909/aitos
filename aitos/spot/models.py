"""Market-neutral, Decimal-safe Binance Spot domain contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

from aitos.market_data.venues import InstrumentKey, MarketType, Venue

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,20}$")
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_:/.-]{1,36}$")


def _decimal(value: Decimal | str | int | float, name: str) -> Decimal:
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


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
        symbol = self.symbol.upper()
        if not _SYMBOL_RE.fullmatch(symbol):
            raise ValueError("invalid Binance Spot symbol")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "base_asset", self.base_asset.upper())
        object.__setattr__(self, "quote_asset", self.quote_asset.upper())
        object.__setattr__(self, "status", self.status.upper())
        object.__setattr__(self, "permissions", tuple(self.permissions))


@dataclass(frozen=True, slots=True)
class SpotBalance:
    asset: str
    free: Decimal
    locked: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset", self.asset.upper())
        free, locked = _decimal(self.free, "free"), _decimal(self.locked, "locked")
        if free < 0 or locked < 0:
            raise ValueError("balance amounts cannot be negative")
        object.__setattr__(self, "free", free)
        object.__setattr__(self, "locked", locked)

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
        symbol = self.symbol.upper()
        if not _SYMBOL_RE.fullmatch(symbol):
            raise ValueError("invalid Binance Spot symbol")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", SpotOrderSide(self.side))
        object.__setattr__(self, "order_type", SpotOrderType(self.order_type))
        quantity = (
            _decimal(self.quantity, "quantity") if self.quantity is not None else None
        )
        quote_quantity = (
            _decimal(self.quote_order_quantity, "quote_order_quantity")
            if self.quote_order_quantity is not None
            else None
        )
        price = _decimal(self.price, "price") if self.price is not None else None
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "quote_order_quantity", quote_quantity)
        object.__setattr__(self, "price", price)
        if (quantity is None) == (quote_quantity is None):
            raise ValueError("provide exactly one of quantity or quote_order_quantity")
        if quantity is not None and quantity <= 0:
            raise ValueError("quantity must be positive")
        if quote_quantity is not None and quote_quantity <= 0:
            raise ValueError("quote_order_quantity must be positive")
        if self.client_order_id and not _CLIENT_ID_RE.fullmatch(self.client_order_id):
            raise ValueError(
                "client_order_id must match Binance's 1-36 character format"
            )
        if self.order_type is SpotOrderType.LIMIT:
            if quote_quantity is not None:
                raise ValueError("LIMIT orders require base quantity")
            if price is None or price <= 0:
                raise ValueError("LIMIT orders require a positive price")
            if self.time_in_force not in {"GTC", "IOC", "FOK"}:
                raise ValueError("LIMIT orders require GTC, IOC or FOK")
        elif price is not None or self.time_in_force is not None:
            raise ValueError("MARKET orders cannot specify price or time_in_force")


@dataclass(frozen=True, slots=True)
class SpotFill:
    price: Decimal
    quantity: Decimal
    commission: Decimal
    commission_asset: str
    trade_id: int | None = None

    def __post_init__(self) -> None:
        price = _decimal(self.price, "fill price")
        quantity = _decimal(self.quantity, "fill quantity")
        commission = _decimal(self.commission, "commission")
        if price <= 0 or quantity <= 0 or commission < 0:
            raise ValueError("invalid Spot fill amounts")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "commission", commission)
        object.__setattr__(self, "commission_asset", self.commission_asset.upper())

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

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "side", SpotOrderSide(self.side))
        if not isinstance(self.status, SpotOrderStatus):
            object.__setattr__(self, "status", SpotOrderStatus(self.status))
        executed = _decimal(self.executed_quantity, "executed_quantity")
        quote = _decimal(self.cumulative_quote_quantity, "cumulative_quote_quantity")
        if executed < 0 or quote < 0:
            raise ValueError("executed amounts cannot be negative")
        object.__setattr__(self, "executed_quantity", executed)
        object.__setattr__(self, "cumulative_quote_quantity", quote)
        object.__setattr__(self, "fills", tuple(self.fills))

    @property
    def average_price(self) -> Decimal | None:
        if self.executed_quantity <= 0:
            return None
        return self.cumulative_quote_quantity / self.executed_quantity
