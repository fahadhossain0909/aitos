"""Bounded ClickHouse loader for point-in-time futures replay datasets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
import json

from aitos.models.market import OrderBookSnapshot, TradeTick


@dataclass(frozen=True)
class ClickHouseReplayDataset:
    symbol: str
    start: datetime
    end: datetime
    events: tuple[TradeTick | OrderBookSnapshot, ...]
    funding_events: tuple[dict[str, Any], ...]
    provenance: dict[str, Any]


class ClickHouseReplayLoader:
    """Load persisted observations without forward filling or look-ahead."""

    def __init__(self, repository: Any, *, max_rows: int = 2_000_000) -> None:
        if max_rows < 1:
            raise ValueError("max_rows must be positive")
        self.repository = repository
        self.max_rows = max_rows

    async def load(
        self, symbol: str, start: datetime, end: datetime, *, limit: int | None = None
    ) -> ClickHouseReplayDataset:
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("replay boundaries must be timezone-aware")
        if end <= start:
            raise ValueError("replay end must follow start")
        row_limit = self.max_rows if limit is None else min(int(limit), self.max_rows)
        if row_limit < 1:
            raise ValueError("replay limit must be positive")
        normalized_symbol = symbol.upper()
        parameters = {
            "symbol": normalized_symbol,
            "start": start.astimezone(timezone.utc),
            "end": end.astimezone(timezone.utc),
            "limit": row_limit,
        }
        trades = await self.repository._query(
            """
            SELECT time, symbol, trade_id, price, quantity, side, is_buyer_maker
            FROM trade_ticks
            WHERE symbol = {symbol:String} AND time >= {start:DateTime64(3, 'UTC')}
              AND time < {end:DateTime64(3, 'UTC')}
            ORDER BY time, trade_id LIMIT {limit:UInt32}
            """,
            parameters,
        )
        books = await self.repository._query(
            """
            SELECT time, symbol, bid_levels, ask_levels, last_update_id
            FROM order_book_snapshots
            WHERE symbol = {symbol:String} AND time >= {start:DateTime64(3, 'UTC')}
              AND time < {end:DateTime64(3, 'UTC')}
            ORDER BY time, last_update_id LIMIT {limit:UInt32}
            """,
            parameters,
        )
        depth_rows = await self.repository._query(
            """
            SELECT event_time, final_update_id, payload_json
            FROM depth_events
            WHERE symbol = {symbol:String}
              AND event_time >= {start:DateTime64(3, 'UTC')}
              AND event_time < {end:DateTime64(3, 'UTC')}
            ORDER BY event_time, final_update_id LIMIT {limit:UInt32}
            """,
            parameters,
        )
        depth_by_update = {
            int(row["final_update_id"]): json.loads(row["payload_json"])
            for row in depth_rows
        }
        funding = await self.repository._query(
            """
            SELECT time, funding_rate, mark_price FROM funding_rates
            WHERE symbol = {symbol:String} AND time >= {start:DateTime64(3, 'UTC')}
              AND time < {end:DateTime64(3, 'UTC')}
            ORDER BY time LIMIT {limit:UInt32}
            """,
            parameters,
        )
        events: list[TradeTick | OrderBookSnapshot] = []
        for row in trades:
            events.append(
                TradeTick.from_dict(
                    {
                        "symbol": row["symbol"],
                        "trade_id": int(row["trade_id"]),
                        "price": float(row["price"]),
                        "quantity": float(row["quantity"]),
                        "side": str(row["side"]),
                        "is_buyer_maker": bool(row["is_buyer_maker"]),
                        "timestamp": row["time"].isoformat(),
                    }
                )
            )
        for row in books:
            bids = tuple(
                tuple(map(float, level)) for level in json.loads(row["bid_levels"])
            )
            asks = tuple(
                tuple(map(float, level)) for level in json.loads(row["ask_levels"])
            )
            events.append(
                OrderBookSnapshot(
                    symbol=str(row["symbol"]),
                    bids=bids,
                    asks=asks,
                    last_update_id=int(row["last_update_id"]),
                    timestamp=row["time"],
                    depth_event=depth_by_update.get(int(row["last_update_id"])),
                )
            )
        events.sort(key=lambda event: event.timestamp)
        funding_events = tuple(
            {
                "timestamp": row["time"],
                "funding_rate": float(row["funding_rate"]),
                "mark_price": float(row["mark_price"]),
            }
            for row in funding
        )
        return ClickHouseReplayDataset(
            symbol=normalized_symbol,
            start=parameters["start"],
            end=parameters["end"],
            events=tuple(events),
            funding_events=funding_events,
            provenance={
                "source": "clickhouse.trade_ticks+order_book_snapshots+funding_rates",
                "trade_rows": len(trades),
                "order_book_rows": len(books),
                "funding_rows": len(funding),
                "event_rows": len(events),
                "lookahead_policy": "strict [start,end), no forward fill",
                "depth_event_rows": len(depth_rows),
                "depth_event_history_available": bool(depth_rows),
            },
        )


@dataclass(frozen=True)
class ClickHouseAblationRun:
    dataset: ClickHouseReplayDataset
    report: Any


class ClickHouseHistoricalAblationRunner:
    """Connect real ClickHouse observations to canonical replay and holdout."""

    def __init__(self, repository: Any, **runner_config: Any) -> None:
        from aitos.learning.ablation import HistoricalAblationRunner

        self.loader = ClickHouseReplayLoader(repository)
        self.runner = HistoricalAblationRunner(**runner_config)

    async def run(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        strategies: dict[str, Any],
        *,
        baseline: str = "baseline",
        limit: int | None = None,
    ) -> ClickHouseAblationRun:
        dataset = await self.loader.load(symbol, start, end, limit=limit)
        if not dataset.events:
            raise ValueError("ClickHouse replay dataset contains no market events")

        def mark_price(event: Any) -> float:
            if isinstance(event, TradeTick):
                return float(event.price)
            if event.best_bid > 0 and event.best_ask > 0:
                return (event.best_bid + event.best_ask) / 2.0
            return 0.0

        report = self.runner.run(
            dataset.events,
            strategies,
            mark_price,
            baseline=baseline,
        )
        return ClickHouseAblationRun(dataset=dataset, report=report)
