"""Domain models for market data, mirroring the ClickHouse schema in the
AITOS spec plus funding rate / open interest.

Models are immutable and serializable to/from plain dicts so they can travel
safely through the Event Bus and be reused by live and historical pipelines.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class TradeSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass(frozen=True)
class Kline:
    symbol: str
    timeframe: str
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades_count: int
    taker_buy_volume: float
    taker_buy_quote_volume: float
    is_closed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "open_time": _iso(self.open_time),
            "close_time": _iso(self.close_time),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "quote_volume": self.quote_volume,
            "trades_count": self.trades_count,
            "taker_buy_volume": self.taker_buy_volume,
            "taker_buy_quote_volume": self.taker_buy_quote_volume,
            "is_closed": self.is_closed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Kline:
        return cls(
            symbol=data["symbol"],
            timeframe=data["timeframe"],
            open_time=_dt(data["open_time"]),
            close_time=_dt(data["close_time"]),
            open=float(data["open"]),
            high=float(data["high"]),
            low=float(data["low"]),
            close=float(data["close"]),
            volume=float(data["volume"]),
            quote_volume=float(data["quote_volume"]),
            trades_count=int(data["trades_count"]),
            taker_buy_volume=float(data["taker_buy_volume"]),
            taker_buy_quote_volume=float(data["taker_buy_quote_volume"]),
            is_closed=bool(data.get("is_closed", True)),
        )


@dataclass(frozen=True)
class OrderBookSnapshot:
    symbol: str
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    last_update_id: int
    timestamp: datetime

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid if self.bids and self.asks else 0.0

    @property
    def depth_ratio(self) -> float:
        bid_depth = sum(qty for _, qty in self.bids)
        ask_depth = sum(qty for _, qty in self.asks)
        return bid_depth / ask_depth if ask_depth else float("inf")

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bid_levels": [{"price": p, "qty": q} for p, q in self.bids],
            "ask_levels": [{"price": p, "qty": q} for p, q in self.asks],
            "spread": self.spread,
            "depth_ratio": self.depth_ratio,
            "last_update_id": self.last_update_id,
            "timestamp": _iso(self.timestamp),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OrderBookSnapshot:
        bids = tuple(
            (float(x["price"]), float(x["qty"]))
            for x in data.get("bid_levels", data.get("bids", []))
        )
        asks = tuple(
            (float(x["price"]), float(x["qty"]))
            for x in data.get("ask_levels", data.get("asks", []))
        )
        return cls(
            symbol=data["symbol"],
            bids=bids,
            asks=asks,
            last_update_id=int(data["last_update_id"]),
            timestamp=_dt(data["timestamp"]),
        )


@dataclass(frozen=True)
class TradeTick:
    symbol: str
    trade_id: int
    price: float
    quantity: float
    side: TradeSide
    is_buyer_maker: bool
    timestamp: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "trade_id": self.trade_id,
            "price": self.price,
            "quantity": self.quantity,
            "side": self.side.value,
            "is_buyer_maker": self.is_buyer_maker,
            "timestamp": _iso(self.timestamp),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TradeTick:
        return cls(
            symbol=data["symbol"],
            trade_id=int(data["trade_id"]),
            price=float(data["price"]),
            quantity=float(data["quantity"]),
            side=TradeSide(data["side"]),
            is_buyer_maker=bool(data["is_buyer_maker"]),
            timestamp=_dt(data["timestamp"]),
        )


@dataclass(frozen=True)
class FundingRate:
    symbol: str
    funding_rate: float
    funding_time: datetime
    mark_price: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "funding_rate": self.funding_rate,
            "funding_time": _iso(self.funding_time),
            "mark_price": self.mark_price,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FundingRate:
        return cls(
            symbol=data["symbol"],
            funding_rate=float(data["funding_rate"]),
            funding_time=_dt(data["funding_time"]),
            mark_price=float(data["mark_price"]),
        )


@dataclass(frozen=True)
class OpenInterest:
    symbol: str
    open_interest: float
    timestamp: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "open_interest": float(self.open_interest),
            "timestamp": _iso(self.timestamp),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OpenInterest:
        return cls(
            symbol=data["symbol"],
            open_interest=float(data["open_interest"]),
            timestamp=_dt(data["timestamp"]),
        )
