"""Historical event sources backed by the ProjectAlpha ClickHouse data layer."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import clickhouse_connect

from .cli import HistoricalEvent, _timestamp


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
        self.client = clickhouse_connect.get_client(
            host=host,
            port=port,
            username=username,
            password=password,
            database=database,
        )

    def close(self) -> None:
        self.client.close()

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
                + " AND ".join(filters)  # nosec B608 - filters are fixed parameterized predicates
                + " ORDER BY time LIMIT {limit:UInt32}"
            )
        elif table == "trades":
            sql = (
                "SELECT time, price, quantity, side, trade_id, is_buyer_maker FROM trade_ticks WHERE "
                + " AND ".join(filters)  # nosec B608 - filters are fixed parameterized predicates
                + " ORDER BY time LIMIT {limit:UInt32}"
            )
        else:
            sql = (
                "SELECT time, bid_levels, ask_levels, spread, depth_ratio, last_update_id FROM order_book_snapshots WHERE "
                + " AND ".join(filters)  # nosec B608 - filters are fixed parameterized predicates
                + " ORDER BY time LIMIT {limit:UInt32}"
            )
        result = self.client.query(sql, parameters=parameters)
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
            yield HistoricalEvent(
                _timestamp(data.pop("time")), price, {"symbol": symbol, **data}
            )


def parse_optional_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = _timestamp(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
