"""Compatibility bridge from legacy exchange models to canonical events."""

from __future__ import annotations

from datetime import datetime, timezone

from aitos.models.market import Kline, OrderBookSnapshot as LegacyBook, TradeTick

from .contracts import MarketEvent, MarketEventType, MarketSource


def _received() -> datetime:
    return datetime.now(timezone.utc)


def trade_event(trade: TradeTick, *, market_type: str = "futures", source: MarketSource = MarketSource.WEBSOCKET) -> MarketEvent:
    received = _received()
    return MarketEvent(
        event_type=MarketEventType.TRADE,
        exchange="binance",
        market=market_type,
        symbol=trade.symbol,
        event_time=trade.timestamp,
        payload=trade.to_dict(),
        source=source,
        ingest_time=received,
        sequence=trade.trade_id,
        correlation_id=f"binance:{market_type}:{trade.symbol}:{trade.trade_id}",
        trace_id=trade.symbol,
    )


def book_snapshot_event(book: LegacyBook, *, market_type: str = "futures", source: MarketSource = MarketSource.WEBSOCKET) -> MarketEvent:
    received = _received()
    return MarketEvent(
        event_type=MarketEventType.BOOK_SNAPSHOT,
        exchange="binance",
        market=market_type,
        symbol=book.symbol,
        event_time=book.timestamp,
        payload={
            "bids": [{"price": p, "quantity": q} for p, q in book.bids],
            "asks": [{"price": p, "quantity": q} for p, q in book.asks],
            "last_update_id": book.last_update_id,
        },
        source=source,
        ingest_time=received,
        sequence=book.last_update_id,
        correlation_id=f"binance:{market_type}:{book.symbol}:book:{book.last_update_id}",
        trace_id=book.symbol,
    )


def kline_event(kline: Kline, *, market_type: str = "futures", source: MarketSource = MarketSource.WEBSOCKET) -> MarketEvent:
    received = _received()
    return MarketEvent(
        event_type=MarketEventType.KLINE,
        exchange="binance",
        market=market_type,
        symbol=kline.symbol,
        event_time=kline.close_time,
        payload=kline.to_dict(),
        source=source,
        ingest_time=received,
        correlation_id=f"binance:{market_type}:{kline.symbol}:kline:{kline.timeframe}",
        trace_id=kline.symbol,
    )
