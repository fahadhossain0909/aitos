"""Pure parsing functions: raw Binance USDT-M Futures payloads → AITOS models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aitos.exchange.orderbook import DepthUpdate
from aitos.models.market import (
    FundingRate,
    Kline,
    OpenInterest,
    OrderBookSnapshot,
    TradeSide,
    TradeTick,
)


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _levels(raw_levels: list[list[str]]) -> tuple[tuple[float, float], ...]:
    return tuple((float(p), float(q)) for p, q in raw_levels)


def parse_kline_rest(raw: list[Any], symbol: str, timeframe: str) -> Kline:
    return Kline(
        symbol=symbol,
        timeframe=timeframe,
        open_time=_ms_to_dt(int(raw[0])),
        close_time=_ms_to_dt(int(raw[6])),
        open=float(raw[1]),
        high=float(raw[2]),
        low=float(raw[3]),
        close=float(raw[4]),
        volume=float(raw[5]),
        quote_volume=float(raw[7]),
        trades_count=int(raw[8]),
        taker_buy_volume=float(raw[9]),
        taker_buy_quote_volume=float(raw[10]),
        is_closed=True,
    )


def parse_order_book_rest(raw: dict[str, Any], symbol: str) -> OrderBookSnapshot:
    timestamp = _ms_to_dt(int(raw["E"])) if "E" in raw else datetime.now(timezone.utc)
    return OrderBookSnapshot(
        symbol=symbol,
        bids=_levels(raw["bids"]),
        asks=_levels(raw["asks"]),
        last_update_id=int(raw["lastUpdateId"]),
        timestamp=timestamp,
    )


def parse_trade_rest(raw: dict[str, Any], symbol: str) -> TradeTick:
    is_buyer_maker = bool(raw["isBuyerMaker"])
    return TradeTick(
        symbol=symbol,
        trade_id=int(raw["id"]),
        price=float(raw["price"]),
        quantity=float(raw["qty"]),
        side=TradeSide.SELL if is_buyer_maker else TradeSide.BUY,
        is_buyer_maker=is_buyer_maker,
        timestamp=_ms_to_dt(int(raw["time"])),
    )


def parse_funding_rate_rest(raw: dict[str, Any]) -> FundingRate:
    return FundingRate(
        symbol=raw["symbol"],
        funding_rate=float(raw["lastFundingRate"]),
        funding_time=_ms_to_dt(int(raw["nextFundingTime"])),
        mark_price=float(raw["markPrice"]),
    )


def parse_open_interest_rest(raw: dict[str, Any]) -> OpenInterest:
    return OpenInterest(
        symbol=raw["symbol"],
        open_interest=float(raw["openInterest"]),
        timestamp=_ms_to_dt(int(raw["time"])),
    )


def parse_kline_ws(payload: dict[str, Any]) -> Kline:
    k = payload["k"]
    return Kline(
        symbol=payload["s"],
        timeframe=k["i"],
        open_time=_ms_to_dt(int(k["t"])),
        close_time=_ms_to_dt(int(k["T"])),
        open=float(k["o"]),
        high=float(k["h"]),
        low=float(k["l"]),
        close=float(k["c"]),
        volume=float(k["v"]),
        quote_volume=float(k["q"]),
        trades_count=int(k["n"]),
        taker_buy_volume=float(k["V"]),
        taker_buy_quote_volume=float(k["Q"]),
        is_closed=bool(k["x"]),
    )


def parse_agg_trade_ws(payload: dict[str, Any]) -> TradeTick:
    is_buyer_maker = bool(payload["m"])
    return TradeTick(
        symbol=payload["s"],
        trade_id=int(payload["a"]),
        price=float(payload["p"]),
        quantity=float(payload["q"]),
        side=TradeSide.SELL if is_buyer_maker else TradeSide.BUY,
        is_buyer_maker=is_buyer_maker,
        timestamp=_ms_to_dt(int(payload["T"])),
    )


def parse_trade_ws(payload: dict[str, Any]) -> TradeTick:
    """Parse Binance raw trade events (``<symbol>@trade``)."""
    is_buyer_maker = bool(payload["m"])
    return TradeTick(
        symbol=payload["s"],
        trade_id=int(payload["t"]),
        price=float(payload["p"]),
        quantity=float(payload["q"]),
        side=TradeSide.SELL if is_buyer_maker else TradeSide.BUY,
        is_buyer_maker=is_buyer_maker,
        timestamp=_ms_to_dt(int(payload["T"])),
    )


def parse_depth_ws(payload: dict[str, Any], symbol: str) -> OrderBookSnapshot:
    timestamp = (
        _ms_to_dt(int(payload["T"])) if "T" in payload else datetime.now(timezone.utc)
    )
    return OrderBookSnapshot(
        symbol=symbol,
        bids=_levels(payload["b"]) if "b" in payload else _levels(payload["bids"]),
        asks=_levels(payload["a"]) if "a" in payload else _levels(payload["asks"]),
        last_update_id=int(payload.get("lastUpdateId", payload.get("u", 0))),
        timestamp=timestamp,
    )


def parse_depth_diff_ws(payload: dict[str, Any]) -> DepthUpdate:
    return DepthUpdate(
        first_update_id=int(payload["U"]),
        final_update_id=int(payload["u"]),
        previous_update_id=int(payload.get("pu", 0)),
        bids=_levels(payload.get("b", [])),
        asks=_levels(payload.get("a", [])),
        event_time_ms=int(payload.get("E", payload.get("T", 0))),
    )
