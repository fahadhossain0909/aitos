"""Stream persisted trades and L2 snapshots as the rich historical market model."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Iterator, Optional

import clickhouse_connect

from aitos.logging_setup import get_logger
from aitos.models.market import OrderBookSnapshot, TradeSide, TradeTick

from .cli import _timestamp

logger = get_logger("aitos.backtest.clickhouse_market_source")


class ClickHouseMarketEventSource:
    """Read the canonical persisted market tables without mutating them.

    The rich ProjectAlpha historical runner needs domain-level TradeTick and
    OrderBookSnapshot objects, not the generic price-event wrapper used by the
    lightweight BacktestEngine. This source joins the two persisted streams in
    timestamp order and keeps the replay read-only.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8123,
        username: str = "default",
        password: str = "",  # nosec B107 - empty default is overridden by deployment config
        database: str = "aitos",
    ) -> None:
        try:
            self.client = clickhouse_connect.get_client(
                host=host,
                port=port,
                username=username,
                password=password,
                database=database,
            )
            logger.info(
                "ClickHouse market event source connected",
                extra={
                    "aitos_extra": {
                        "host": host,
                        "port": port,
                        "database": database,
                    }
                },
            )
        except Exception:
            logger.exception(
                "ClickHouse market event source connection failed",
                extra={
                    "aitos_extra": {
                        "host": host,
                        "port": port,
                        "database": database,
                    }
                },
            )
            raise

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            logger.exception("ClickHouse market event source close failed")

    def _bounds(
        self, start: Optional[datetime], end: Optional[datetime]
    ) -> tuple[str, dict]:
        filters = ["symbol = {symbol:String}"]
        params: dict = {}
        if start is not None:
            filters.append("time >= {start:DateTime64(3)}")
            params["start"] = start
        if end is not None:
            filters.append("time < {end:DateTime64(3)}")
            params["end"] = end
        return " AND ".join(filters), params

    def events(
        self,
        symbol: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 10_000_000,
    ) -> Iterator[TradeTick | OrderBookSnapshot]:
        where, params = self._bounds(start, end)
        params = {**params, "symbol": symbol, "limit": limit}
        logger.info(
            "ClickHouse market event query started",
            extra={
                "aitos_extra": {
                    "symbol": symbol,
                    "start": start.isoformat() if start else None,
                    "end": end.isoformat() if end else None,
                    "limit": limit,
                }
            },
        )
        try:
            trade = self.client.query(
                "SELECT time, price, quantity, side, trade_id, is_buyer_maker "
                "FROM trade_ticks WHERE "
                + where  # nosec B608 - where contains only fixed parameterized predicates
                + " ORDER BY time, trade_id LIMIT {limit:UInt32}",
                parameters=params,
            )
            book = self.client.query(
                "SELECT time, bid_levels, ask_levels, last_update_id "
                "FROM order_book_snapshots WHERE "
                + where  # nosec B608 - where contains only fixed parameterized predicates
                + " ORDER BY time, last_update_id LIMIT {limit:UInt32}",
                parameters=params,
            )
        except Exception:
            logger.exception(
                "ClickHouse market event query failed",
                extra={"aitos_extra": {"symbol": symbol, "limit": limit}},
            )
            raise

        events: list[TradeTick | OrderBookSnapshot] = []
        for row in trade.result_rows:
            time, price, quantity, side, trade_id, is_buyer_maker = row
            events.append(
                TradeTick(
                    symbol=symbol,
                    trade_id=int(trade_id),
                    price=float(price),
                    quantity=float(quantity),
                    side=TradeSide(str(side)),
                    is_buyer_maker=bool(is_buyer_maker),
                    timestamp=_timestamp(time),
                )
            )
        for row in book.result_rows:
            time, bids_raw, asks_raw, update_id = row
            bids_data = (
                json.loads(bids_raw or "[]") if isinstance(bids_raw, str) else bids_raw
            )
            asks_data = (
                json.loads(asks_raw or "[]") if isinstance(asks_raw, str) else asks_raw
            )
            bids = tuple(
                (
                    float(x["price"] if isinstance(x, dict) else x[0]),
                    float(x["qty"] if isinstance(x, dict) else x[1]),
                )
                for x in bids_data
            )
            asks = tuple(
                (
                    float(x["price"] if isinstance(x, dict) else x[0]),
                    float(x["qty"] if isinstance(x, dict) else x[1]),
                )
                for x in asks_data
            )
            events.append(
                OrderBookSnapshot(
                    symbol=symbol,
                    bids=bids,
                    asks=asks,
                    last_update_id=int(update_id),
                    timestamp=_timestamp(time),
                )
            )
        events.sort(key=lambda event: event.timestamp)

        trade_count = len(trade.result_rows)
        book_count = len(book.result_rows)
        if not events:
            logger.warning(
                "ClickHouse market event query returned no rows",
                extra={
                    "aitos_extra": {
                        "symbol": symbol,
                        "start": start.isoformat() if start else None,
                        "end": end.isoformat() if end else None,
                    }
                },
            )
        else:
            logger.info(
                "ClickHouse market event query completed",
                extra={
                    "aitos_extra": {
                        "symbol": symbol,
                        "trades": trade_count,
                        "orderbook_snapshots": book_count,
                        "total_events": len(events),
                    }
                },
            )
        yield from events
