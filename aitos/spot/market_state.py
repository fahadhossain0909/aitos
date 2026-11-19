"""Spot-only live feature state and candidate construction.

Subscriptions use market.spot.* channels, so this consumer cannot ingest
same-symbol Futures events. It builds observable pre-filter candidates; it does
not create trade signals or bypass the shared Strategy/Capital/EIE authorities.
"""
from __future__ import annotations

import asyncio
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from statistics import pstdev
from typing import Any

from aitos.market_data.bus import MarketDataBus
from aitos.market_data.contracts import MarketEvent, MarketEventType
from aitos.spot.scanner import RankedSpotCandidate, SpotCandidate, SpotOpportunityScanner

SPOT_STATE_GROUP = "spot-live-feature-state-v1"


@dataclass(slots=True)
class SpotMarketState:
    symbol: str
    trades: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=2000))
    klines: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=200))
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    bid_quantity: Decimal = Decimal("0")
    ask_quantity: Decimal = Decimal("0")
    last_update_id: int | None = None
    book_valid: bool = False
    last_trade_at: datetime | None = None
    last_ticker_at: datetime | None = None
    last_book_at: datetime | None = None
    last_kline_at: datetime | None = None
    sequence_gaps: int = 0


class SpotMarketDataConsumer:
    """Maintain a bounded Spot-only state cache and rank current candidates."""

    def __init__(
        self,
        scanner: SpotOpportunityScanner,
        *,
        max_symbols: int = 100,
        ticker_freshness_seconds: float = 15.0,
        book_freshness_seconds: float = 15.0,
        kline_freshness_seconds: float = 120.0,
    ) -> None:
        if not 1 <= max_symbols <= 100:
            raise ValueError("max_symbols must be between 1 and 100")
        self.scanner = scanner
        self.max_symbols = max_symbols
        self.ticker_freshness_seconds = ticker_freshness_seconds
        self.book_freshness_seconds = book_freshness_seconds
        self.kline_freshness_seconds = kline_freshness_seconds
        self._states: dict[str, SpotMarketState] = {}
        self._subscriptions = []
        self._started = False
        self._events = 0
        self._rejected = 0

    @property
    def states(self) -> dict[str, SpotMarketState]:
        return dict(self._states)

    def state(self, symbol: str) -> SpotMarketState | None:
        return self._states.get(symbol.upper())

    async def start(self, market_bus: MarketDataBus) -> None:
        if self._started:
            return
        handlers = {
            MarketEventType.TRADE: self._on_trade,
            MarketEventType.TICKER: self._on_ticker,
            MarketEventType.BOOK_SNAPSHOT: self._on_book_snapshot,
            MarketEventType.BOOK_DELTA: self._on_book_delta,
            MarketEventType.KLINE: self._on_kline,
        }
        try:
            for event_type, handler in handlers.items():
                self._subscriptions.append(
                    await market_bus.subscribe(
                        event_type,
                        handler,
                        group=SPOT_STATE_GROUP,
                        live_only=True,
                        market_type="spot",
                    )
                )
        except Exception:
            await self.stop()
            raise
        self._started = True

    async def stop(self) -> None:
        subscriptions, self._subscriptions = self._subscriptions, []
        for subscription in subscriptions:
            subscription.cancel()
        tasks = [
            getattr(subscription, "_task", None)
            for subscription in subscriptions
        ]
        await asyncio.gather(
            *(task for task in tasks if task is not None),
            return_exceptions=True,
        )
        self._started = False

    def _state(self, symbol: str) -> SpotMarketState:
        key = symbol.upper()
        if key not in self._states:
            if len(self._states) >= self.max_symbols:
                raise RuntimeError("Spot live-state symbol capacity exceeded")
            self._states[key] = SpotMarketState(key)
        return self._states[key]

    async def _on_trade(self, event: MarketEvent) -> None:
        if event.market_type != "spot":
            return
        state = self._state(event.symbol)
        payload = event.payload
        state.trades.append(
            {
                "price": Decimal(str(payload["price"])),
                "quantity": Decimal(str(payload["quantity"])),
                "buyer_is_maker": bool(payload["buyer_is_maker"]),
                "event_time": event.event_time,
            }
        )
        state.last_trade_at = event.event_time
        self._events += 1

    async def _on_ticker(self, event: MarketEvent) -> None:
        if event.market_type != "spot":
            return
        state = self._state(event.symbol)
        payload = event.payload
        bid = Decimal(str(payload["bid_price"]))
        ask = Decimal(str(payload["ask_price"]))
        if bid <= 0 or ask <= bid:
            self._rejected += 1
            return
        state.best_bid = bid
        state.best_ask = ask
        state.bid_quantity = Decimal(str(payload.get("bid_quantity", "0")))
        state.ask_quantity = Decimal(str(payload.get("ask_quantity", "0")))
        state.last_ticker_at = event.event_time
        self._events += 1

    async def _on_book_snapshot(self, event: MarketEvent) -> None:
        if event.market_type != "spot":
            return
        state = self._state(event.symbol)
        bids = {
            Decimal(str(level["price"])): Decimal(str(level["quantity"]))
            for level in event.payload.get("bids", [])
            if Decimal(str(level["price"])) > 0
            and Decimal(str(level["quantity"])) > 0
        }
        asks = {
            Decimal(str(level["price"])): Decimal(str(level["quantity"]))
            for level in event.payload.get("asks", [])
            if Decimal(str(level["price"])) > 0
            and Decimal(str(level["quantity"])) > 0
        }
        if not bids or not asks or max(bids) >= min(asks):
            state.book_valid = False
            self._rejected += 1
            return
        state.bids, state.asks = bids, asks
        state.last_update_id = int(event.payload["last_update_id"])
        state.book_valid = True
        state.last_book_at = event.event_time
        self._events += 1

    async def _on_book_delta(self, event: MarketEvent) -> None:
        if event.market_type != "spot":
            return
        state = self._state(event.symbol)
        payload = event.payload
        if not state.book_valid or state.last_update_id is None:
            self._rejected += 1
            return
        first = int(payload["first_update_id"])
        final = int(payload["final_update_id"])
        previous = payload.get("previous_update_id")
        if final <= state.last_update_id:
            return
        if (
            (previous is not None and int(previous) != state.last_update_id)
            or (previous is None and first > state.last_update_id + 1)
        ):
            state.book_valid = False
            state.sequence_gaps += 1
            state.bids.clear()
            state.asks.clear()
            self._rejected += 1
            return
        self._apply_levels(state.bids, payload.get("bids", []))
        self._apply_levels(state.asks, payload.get("asks", []))
        state.bids = dict(sorted(state.bids.items(), reverse=True)[:100])
        state.asks = dict(sorted(state.asks.items())[:100])
        if not state.bids or not state.asks or max(state.bids) >= min(state.asks):
            state.book_valid = False
            state.sequence_gaps += 1
            self._rejected += 1
            return
        state.last_update_id = final
        state.last_book_at = event.event_time
        self._events += 1

    async def _on_kline(self, event: MarketEvent) -> None:
        if event.market_type != "spot" or not event.payload.get("is_closed", False):
            return
        state = self._state(event.symbol)
        state.klines.append(
            {
                "close": Decimal(str(event.payload["close"])),
                "quote_volume": Decimal(str(event.payload["quote_volume"])),
                "close_time": event.event_time,
            }
        )
        state.last_kline_at = event.event_time
        self._events += 1

    @staticmethod
    def _apply_levels(
        book: dict[Decimal, Decimal], updates: list[dict[str, Any]]
    ) -> None:
        for level in updates:
            price = Decimal(str(level["price"]))
            quantity = Decimal(str(level["quantity"]))
            if quantity <= 0:
                book.pop(price, None)
            else:
                book[price] = quantity

    def build_candidate(
        self,
        symbol: str,
        *,
        quote_volume_24h: Decimal,
        now: datetime | None = None,
    ) -> SpotCandidate | None:
        state = self._states.get(symbol.upper())
        if state is None or not state.book_valid:
            return None
        now = now or datetime.now(timezone.utc)
        if state.best_bid is None or state.best_ask is None:
            if not state.bids or not state.asks:
                return None
            bid, ask = max(state.bids), min(state.asks)
        else:
            bid, ask = state.best_bid, state.best_ask
        if bid <= 0 or ask <= bid:
            return None
        if not self._fresh(state.last_ticker_at, now, self.ticker_freshness_seconds):
            return None
        if not self._fresh(state.last_book_at, now, self.book_freshness_seconds):
            return None
        if not self._fresh(state.last_kline_at, now, self.kline_freshness_seconds):
            return None
        if len(state.klines) < 21:
            return None

        candles = list(state.klines)
        closes = [float(row["close"]) for row in candles[-21:]]
        if any(value <= 0 for value in closes):
            return None
        returns = [
            math.log(closes[index] / closes[index - 1])
            for index in range(1, len(closes))
        ]
        volatility_bps = Decimal(str(pstdev(returns) * 10000.0))
        momentum_bps = Decimal(str((closes[-1] / closes[-6] - 1.0) * 10000.0))
        previous_volumes = [
            row["quote_volume"] for row in candles[-21:-1]
        ]
        baseline = sum(previous_volumes, Decimal("0")) / Decimal(
            len(previous_volumes)
        )
        if baseline <= 0:
            return None
        relative_volume = candles[-1]["quote_volume"] / baseline

        top_bids = sorted(state.bids.items(), reverse=True)[:10]
        top_asks = sorted(state.asks.items())[:10]
        bid_depth = sum((qty for _, qty in top_bids), Decimal("0"))
        ask_depth = sum((qty for _, qty in top_asks), Decimal("0"))
        total_depth = bid_depth + ask_depth
        if total_depth <= 0:
            return None
        imbalance = (bid_depth - ask_depth) / total_depth
        mid = (bid + ask) / Decimal("2")
        spread_bps = (ask - bid) / mid * Decimal("10000")
        return SpotCandidate(
            symbol=state.symbol,
            quote_volume=quote_volume_24h,
            relative_volume=relative_volume,
            spread_bps=spread_bps,
            volatility_bps=volatility_bps,
            momentum_bps=momentum_bps,
            order_book_imbalance=imbalance,
            trading_enabled=True,
            data_fresh=True,
        )

    def rank_candidates(
        self, quote_volumes_24h: dict[str, Decimal]
    ) -> list[RankedSpotCandidate]:
        candidates = []
        for symbol, volume in quote_volumes_24h.items():
            candidate = self.build_candidate(
                symbol, quote_volume_24h=Decimal(str(volume))
            )
            if candidate is not None:
                candidates.append(candidate)
        return self.scanner.rank(candidates)

    @staticmethod
    def _fresh(
        timestamp: datetime | None,
        now: datetime,
        maximum_age_seconds: float,
    ) -> bool:
        if timestamp is None:
            return False
        age = (now - timestamp).total_seconds()
        return -1.0 <= age <= maximum_age_seconds

    def health(self) -> dict[str, Any]:
        return {
            "started": self._started,
            "symbols": len(self._states),
            "events": self._events,
            "rejected": self._rejected,
            "book_sequence_gaps": sum(
                state.sequence_gaps for state in self._states.values()
            ),
            "valid_books": sum(
                1 for state in self._states.values() if state.book_valid
            ),
        }
