"""Bounded ClickHouse loader for point-in-time futures replay datasets."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from inspect import signature
from typing import Any

from aitos.exchange.orderbook import DepthUpdate, LocalOrderBook
from aitos.intelligence.liquidity import depth_event_flow_analysis
from aitos.intelligence.live_scanner import LiveScannerCache
from aitos.models.market import OrderBookSnapshot, TradeTick


@dataclass(frozen=True)
class ClickHouseReplayDataset:
    symbol: str
    start: datetime
    end: datetime
    events: tuple[TradeTick | OrderBookSnapshot, ...]
    funding_events: tuple[dict[str, Any], ...]
    candles: tuple[dict[str, Any], ...]
    provenance: dict[str, Any]

    def candles_as_of(self, timestamp: datetime) -> tuple[dict[str, Any], ...]:
        """Return only fully closed candles known by the supplied event time."""
        cutoff = timestamp
        if cutoff.tzinfo is None:
            raise ValueError("candle decision timestamp must be timezone-aware")
        return tuple(
            candle for candle in self.candles if candle["close_time"] <= cutoff
        )


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
        candle_start = parameters["start"] - timedelta(days=90)
        candle_parameters = {**parameters, "candle_start": candle_start}
        candles = await self.repository._query(
            """
            SELECT time, timeframe, open, high, low, close, volume,
                   quote_volume, trades_count, taker_buy_volume,
                   taker_buy_quote_volume
            FROM market_ohlcv
            WHERE symbol = {symbol:String}
              AND time >= {candle_start:DateTime64(3, 'UTC')}
              AND time < {end:DateTime64(3, 'UTC')}
            ORDER BY time, timeframe LIMIT {limit:UInt32}
            """,
            candle_parameters,
        )
        bootstrap_rows = await self.repository._query(
            """
            SELECT time, symbol, bid_levels, ask_levels, last_update_id
            FROM order_book_snapshots
            WHERE symbol = {symbol:String} AND time < {start:DateTime64(3, 'UTC')}
            ORDER BY time DESC, last_update_id DESC LIMIT 1
            """,
            parameters,
        )
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
        depth_start = (
            bootstrap_rows[0]["time"] if bootstrap_rows else parameters["start"]
        )
        depth_parameters = {**parameters, "depth_start": depth_start}
        depth_rows = await self.repository._query(
            """
            SELECT event_time, final_update_id, payload_json
            FROM depth_events
            WHERE symbol = {symbol:String}
              AND event_time >= {depth_start:DateTime64(3, 'UTC')}
              AND event_time < {end:DateTime64(3, 'UTC')}
            ORDER BY event_time, final_update_id LIMIT {limit:UInt32}
            """,
            depth_parameters,
        )
        depth_payloads = [(row, json.loads(row["payload_json"])) for row in depth_rows]
        depth_by_update = {
            int(row["final_update_id"]): payload
            for row, payload in depth_payloads
            if "ofi_raw" in payload or "bid_reduce_qty" in payload
        }
        replayed_depth: list[OrderBookSnapshot] = []
        if bootstrap_rows:
            bootstrap = bootstrap_rows[0]
            bootstrap_book = OrderBookSnapshot(
                symbol=str(bootstrap["symbol"]),
                bids=tuple(
                    tuple(map(float, level))
                    for level in json.loads(bootstrap["bid_levels"])
                ),
                asks=tuple(
                    tuple(map(float, level))
                    for level in json.loads(bootstrap["ask_levels"])
                ),
                last_update_id=int(bootstrap["last_update_id"]),
                timestamp=bootstrap["time"],
            )
            local_book = LocalOrderBook(normalized_symbol, max_levels=100)
            local_book.seed(bootstrap_book)
            for row, payload in depth_payloads:
                has_bid_delta = "bids" in payload or "b" in payload
                has_ask_delta = "asks" in payload or "a" in payload
                if not (has_bid_delta and has_ask_delta):
                    continue
                raw_bids = payload.get("bids", payload.get("b", ()))
                raw_asks = payload.get("asks", payload.get("a", ()))
                first_id = payload.get("first_update_id", payload.get("U"))
                final_id = payload.get("final_update_id", payload.get("u"))
                if first_id is None or final_id is None:
                    continue

                def levels(items: Any) -> tuple[tuple[float, float], ...]:
                    converted = []
                    for item in items:
                        if isinstance(item, dict):
                            price = item.get("price")
                            quantity = item.get("quantity", item.get("qty"))
                        else:
                            price, quantity = item[0], item[1]
                        converted.append((float(price), float(quantity)))
                    return tuple(converted)

                previous_id = payload.get(
                    "previous_update_id", payload.get("pu", max(0, int(first_id) - 1))
                )
                update = DepthUpdate(
                    first_update_id=int(first_id),
                    final_update_id=int(final_id),
                    previous_update_id=int(previous_id),
                    bids=levels(raw_bids),
                    asks=levels(raw_asks),
                    event_time_ms=int(row["event_time"].timestamp() * 1000),
                )
                snapshot = local_book.apply(update)
                if (
                    snapshot is not None
                    and snapshot.depth_event is not None
                    and snapshot.timestamp >= parameters["start"]
                ):
                    replayed_depth.append(snapshot)
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
        if not replayed_depth:
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
        events.extend(replayed_depth)
        events.sort(key=lambda event: event.timestamp)
        funding_events = tuple(
            {
                "timestamp": row["time"],
                "funding_rate": float(row["funding_rate"]),
                "mark_price": float(row["mark_price"]),
            }
            for row in funding
        )
        timeframe_durations = {
            "1m": timedelta(minutes=1),
            "3m": timedelta(minutes=3),
            "5m": timedelta(minutes=5),
            "15m": timedelta(minutes=15),
            "30m": timedelta(minutes=30),
            "1h": timedelta(hours=1),
            "2h": timedelta(hours=2),
            "4h": timedelta(hours=4),
            "6h": timedelta(hours=6),
            "8h": timedelta(hours=8),
            "12h": timedelta(hours=12),
            "1d": timedelta(days=1),
        }
        candle_rows = []
        for row in candles:
            open_time = row["time"]
            if open_time.tzinfo is None:
                raise ValueError("historical candle timestamps must be timezone-aware")
            timeframe = str(row["timeframe"]).lower()
            duration = timeframe_durations.get(timeframe)
            if duration is None:
                continue
            candle_rows.append(
                {
                    "open_time": open_time,
                    "close_time": open_time + duration,
                    "timeframe": timeframe,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                    "quote_volume": float(row.get("quote_volume", 0.0)),
                    "trades_count": int(row.get("trades_count", 0)),
                    "taker_buy_volume": float(row.get("taker_buy_volume", 0.0)),
                    "taker_buy_quote_volume": float(
                        row.get("taker_buy_quote_volume", 0.0)
                    ),
                }
            )
        return ClickHouseReplayDataset(
            symbol=normalized_symbol,
            start=parameters["start"],
            end=parameters["end"],
            events=tuple(events),
            funding_events=funding_events,
            candles=tuple(candle_rows),
            provenance={
                "source": "clickhouse.trade_ticks+order_book_snapshots+funding_rates",
                "trade_rows": len(trades),
                "order_book_rows": len(books),
                "funding_rows": len(funding),
                "candle_rows": len(candle_rows),
                "candle_lookback_days": 90,
                "event_rows": len(events),
                "lookahead_policy": "strict [start,end), no forward fill",
                "depth_event_rows": len(depth_rows),
                "depth_replay_bootstrap_time": (
                    depth_start.isoformat() if bootstrap_rows else None
                ),
                "raw_depth_events_replayed": len(replayed_depth),
                "raw_depth_event_replay_available": bool(replayed_depth),
                "derived_depth_snapshot_count": len(depth_by_update),
                "depth_event_history_available": bool(replayed_depth)
                or bool(depth_by_update),
            },
        )


async def replay_depth_flow(
    dataset: ClickHouseReplayDataset, *, min_fresh_events: int = 3
) -> dict[str, Any]:
    """Replay loaded L2 snapshots and trades through the live OFI consumer."""
    cache = LiveScannerCache(None, [dataset.symbol])
    for event in dataset.events:
        if isinstance(event, TradeTick):
            await cache.accept_live_trade(event)
        elif isinstance(event, OrderBookSnapshot) and event.depth_event:
            await cache.accept_live_order_book(event)
    state = cache.snapshot(dataset.symbol)
    if state is None:
        return depth_event_flow_analysis(
            [],
            now_ms=int(dataset.end.timestamp() * 1000),
            min_fresh_events=min_fresh_events,
        )
    cutoff_ms = int(dataset.end.timestamp() * 1000)
    state.trade_depth_matcher.advance_watermark(cutoff_ms)
    replay_window_ms = max(
        1500, int((dataset.end - dataset.start).total_seconds() * 1000)
    )
    return depth_event_flow_analysis(
        list(state.depth_event_history),
        now_ms=cutoff_ms,
        max_age_ms=replay_window_ms,
        min_fresh_events=min_fresh_events,
    )


@dataclass(frozen=True)
class ClickHouseAblationRun:
    dataset: ClickHouseReplayDataset
    report: Any
    depth_flow: dict[str, Any]


@dataclass(frozen=True)
class ClickHouseAMDReplayRun:
    dataset: ClickHouseReplayDataset
    observations: tuple[Any, ...]
    cohorts: tuple[dict[str, Any], ...]
    timeframe: str
    horizon_bars: int


class ClickHouseHistoricalAblationRunner:
    """Connect real ClickHouse observations to canonical replay and holdout."""

    def __init__(
        self,
        repository: Any,
        *,
        required_timeframes: tuple[str, ...] = (
            "1m",
            "5m",
            "15m",
            "1h",
            "4h",
            "1d",
        ),
        min_candles_per_timeframe: int = 20,
        **runner_config: Any,
    ) -> None:
        from aitos.learning.ablation import HistoricalAblationRunner

        if min_candles_per_timeframe < 1:
            raise ValueError("min_candles_per_timeframe must be positive")
        self.loader = ClickHouseReplayLoader(repository)
        self.runner = HistoricalAblationRunner(**runner_config)
        self.required_timeframes = tuple(
            timeframe.lower() for timeframe in required_timeframes
        )
        self.min_candles_per_timeframe = min_candles_per_timeframe

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
        if not dataset.provenance["raw_depth_event_replay_available"]:
            raise ValueError(
                "point-in-time OFI ablation requires raw sequenced depth deltas "
                "and a pre-window order-book bootstrap snapshot"
            )
        candle_counts = {
            timeframe: sum(
                candle["timeframe"] == timeframe for candle in dataset.candles
            )
            for timeframe in self.required_timeframes
        }
        insufficient = {
            timeframe: count
            for timeframe, count in candle_counts.items()
            if count < self.min_candles_per_timeframe
        }
        if insufficient:
            raise ValueError(
                "point-in-time MTF replay lacks required closed-candle history: "
                + repr(insufficient)
            )
        depth_flow = await replay_depth_flow(dataset)
        if not depth_flow.get("ofi_available"):
            raise ValueError(
                "point-in-time OFI replay is unavailable: "
                + str(depth_flow.get("reason", "insufficient fresh depth events"))
            )

        def mark_price(event: Any) -> float:
            if isinstance(event, TradeTick):
                return float(event.price)
            if event.best_bid > 0 and event.best_ask > 0:
                return (event.best_bid + event.best_ask) / 2.0
            raise ValueError("historical replay event has no valid point-in-time mark")

        point_in_time_strategies = {}
        for name, strategy in strategies.items():
            try:
                accepts_context = len(signature(strategy).parameters) >= 3
            except (TypeError, ValueError):
                accepts_context = False
            if accepts_context:

                def with_context(
                    event: Any, execution: Any, *, _strategy=strategy
                ) -> Any:
                    context = {
                        "candles": dataset.candles_as_of(event.timestamp),
                        "funding_events": tuple(
                            item
                            for item in dataset.funding_events
                            if item["timestamp"] <= event.timestamp
                        ),
                    }
                    return _strategy(event, execution, context)

                point_in_time_strategies[name] = with_context
            else:
                point_in_time_strategies[name] = strategy
        report = self.runner.run(
            dataset.events,
            point_in_time_strategies,
            mark_price,
            baseline=baseline,
        )
        return ClickHouseAblationRun(
            dataset=dataset,
            report=report,
            depth_flow=depth_flow,
        )

    async def run_amd_phase_replay(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        timeframe: str,
        horizon_bars: int = 20,
        limit: int | None = None,
    ) -> ClickHouseAMDReplayRun:
        """Load and replay point-in-time AMD observations from ClickHouse."""
        import asyncio

        from aitos.intelligence.amd_research import (
            replay_amd_historical_observations,
            summarize_amd_forward_cohorts,
        )

        dataset = await self.loader.load(symbol, start, end, limit=limit)
        observations = await asyncio.to_thread(
            replay_amd_historical_observations,
            dataset,
            timeframe=timeframe,
            horizon_bars=horizon_bars,
        )
        if not observations:
            raise ValueError(
                "AMD historical replay produced no observations; check candle "
                "coverage, timeframe and the 20-closed-candle warm-up"
            )
        return ClickHouseAMDReplayRun(
            dataset=dataset,
            observations=observations,
            cohorts=summarize_amd_forward_cohorts(observations),
            timeframe=timeframe.lower(),
            horizon_bars=horizon_bars,
        )
