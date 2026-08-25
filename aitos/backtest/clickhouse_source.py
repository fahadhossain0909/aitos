"""Historical event sources backed by the ProjectAlpha ClickHouse data layer."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import clickhouse_connect

from aitos.logging_setup import get_logger

from .cli import HistoricalEvent, _timestamp

logger = get_logger("aitos.backtest.clickhouse_source")


class ClickHouseHistoricalSource:
    """Stream persisted market history into the canonical BacktestEngine.

    ClickHouse is the preferred long-lived source. The source never mutates
    production data and only selects a bounded time window.
    """

    TABLES = {
        "ohlcv": "market_ohlcv",
        "trades": "trade_ticks",
        "orderbook": "order_book_snapshots",
    }

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
                "ClickHouse historical source connected",
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
                "ClickHouse historical source connection failed",
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
            logger.exception("ClickHouse historical source close failed")

    def events(
        self,
        symbol: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        table: str = "ohlcv",
        timeframe: str = "15m",
        limit: int = 10_000_000,
    ) -> Iterator[HistoricalEvent]:
        if table not in self.TABLES:
            raise ValueError(f"unsupported ClickHouse table: {table}")
        source_table = self.TABLES[table]
        filters = ["symbol = {symbol:String}"]
        parameters: dict[str, Any] = {"symbol": symbol, "limit": limit}
        if start is not None:
            filters.append("time >= {start:DateTime64(3)}")
            parameters["start"] = start
        if end is not None:
            filters.append("time < {end:DateTime64(3)}")
            parameters["end"] = end
        if table == "ohlcv":
            filters.append("timeframe = {timeframe:String}")
            parameters["timeframe"] = timeframe
            sql = (
                "SELECT time, open, high, low, close, volume, quote_volume, trades_count FROM market_ohlcv WHERE "
                + " AND ".join(
                    filters
                )  # nosec B608 - filters are fixed parameterized predicates
                + " ORDER BY time LIMIT {limit:UInt32}"
            )
        elif table == "trades":
            sql = (
                "SELECT time, price, quantity, side, trade_id, is_buyer_maker FROM trade_ticks WHERE "
                + " AND ".join(
                    filters
                )  # nosec B608 - filters are fixed parameterized predicates
                + " ORDER BY time LIMIT {limit:UInt32}"
            )
        else:
            sql = (
                "SELECT time, bid_levels, ask_levels, spread, depth_ratio, last_update_id FROM order_book_snapshots WHERE "
                + " AND ".join(
                    filters
                )  # nosec B608 - filters are fixed parameterized predicates
                + " ORDER BY time LIMIT {limit:UInt32}"
            )
        logger.info(
            "ClickHouse historical query started",
            extra={
                "aitos_extra": {
                    "symbol": symbol,
                    "table": source_table,
                    "timeframe": timeframe if table == "ohlcv" else None,
                    "start": start.isoformat() if start else None,
                    "end": end.isoformat() if end else None,
                    "limit": limit,
                }
            },
        )
        try:
            result = self.client.query(sql, parameters=parameters)
        except Exception:
            logger.exception(
                "ClickHouse historical query failed",
                extra={
                    "aitos_extra": {
                        "symbol": symbol,
                        "table": source_table,
                        "limit": limit,
                    }
                },
            )
            raise

        row_count = 0
        for row in result.result_rows:
            data = dict(zip(result.column_names, row))
            if table == "orderbook":
                bids = json.loads(data.pop("bid_levels") or "[]")
                asks = json.loads(data.pop("ask_levels") or "[]")
                best_bid = float(bids[0][0]) if bids else 0.0
                best_ask = float(asks[0][0]) if asks else 0.0
                price = (
                    (best_bid + best_ask) / 2
                    if best_bid and best_ask
                    else (best_bid or best_ask)
                )
                data.update({"bids": bids, "asks": asks})
            else:
                price = float(data["close"] if table == "ohlcv" else data["price"])
            row_count += 1
            yield HistoricalEvent(
                _timestamp(data.pop("time")), price, {"symbol": symbol, **data}
            )

        if row_count == 0:
            logger.warning(
                "ClickHouse historical query returned no rows",
                extra={
                    "aitos_extra": {
                        "symbol": symbol,
                        "table": source_table,
                        "start": start.isoformat() if start else None,
                        "end": end.isoformat() if end else None,
                    }
                },
            )
        else:
            logger.info(
                "ClickHouse historical query completed",
                extra={
                    "aitos_extra": {
                        "symbol": symbol,
                        "table": source_table,
                        "rows": row_count,
                    }
                },
            )


def parse_optional_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = _timestamp(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
