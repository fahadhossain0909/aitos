"""Bounded Binance producers for canonical auxiliary market-data channels."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from .binance_adapter import BinanceCanonicalMarketDataAdapter
from .contracts import MarketEvent, MarketEventType, MarketSource

TICKER_TIMEFRAME = "1s"
OPEN_INTEREST_POLL_SECONDS = 15.0
INSTRUMENT_POLL_SECONDS = 300.0
TOP_SYMBOL_LIMIT = 50


def _dt(ms: float | str | None) -> datetime:
    if ms is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)


def _event(
    event_type: MarketEventType,
    symbol: str,
    payload: dict[str, Any],
    event_time: datetime,
) -> MarketEvent:
    return MarketEvent(
        event_type=event_type,
        exchange="binance",
        market="usd_m_futures",
        symbol=symbol,
        event_time=event_time,
        payload=payload,
        source=(
            MarketSource.WEBSOCKET
            if event_type
            not in {MarketEventType.OPEN_INTEREST, MarketEventType.INSTRUMENT}
            else MarketSource.REST
        ),
        correlation_id=f"binance:usd_m_futures:{symbol}:{event_type.value}:{event_time.timestamp()}",
        trace_id=symbol,
    )


class BinanceAuxiliaryMarketDataAdapter(BinanceCanonicalMarketDataAdapter):
    """Real producers for ticker/funding/liquidation and REST-polled OI/instruments."""

    async def stream_tickers(self, symbols: list[str]) -> AsyncIterator[MarketEvent]:
        bounded = list(dict.fromkeys(s.upper() for s in symbols if s))[
            :TOP_SYMBOL_LIMIT
        ]
        streams = [f"{s.lower()}@ticker" for s in bounded]
        async for data, stream_name in self.exchange._raw_stream(
            streams, emit_reconnect=True
        ):
            symbol = stream_name.split("@", 1)[0].upper()
            yield _event(MarketEventType.TICKER, symbol, dict(data), _dt(data.get("E")))

    async def stream_funding(self, symbols: list[str]) -> AsyncIterator[MarketEvent]:
        bounded = list(dict.fromkeys(s.upper() for s in symbols if s))[
            :TOP_SYMBOL_LIMIT
        ]
        streams = [f"{s.lower()}@markPrice@1s" for s in bounded]
        async for data, stream_name in self.exchange._raw_stream(
            streams, emit_reconnect=True
        ):
            symbol = stream_name.split("@", 1)[0].upper()
            payload = {
                "symbol": symbol,
                "funding_rate": float(data.get("r", 0.0)),
                "mark_price": float(data.get("p", 0.0)),
                "index_price": float(data.get("i", 0.0)),
                "next_funding_time": data.get("T"),
                "event_time": data.get("E"),
            }
            yield _event(MarketEventType.FUNDING, symbol, payload, _dt(data.get("E")))

    async def stream_liquidations(
        self, symbols: list[str] | None = None
    ) -> AsyncIterator[MarketEvent]:
        allowed = {s.upper() for s in (symbols or [])}
        async for data, _ in self.exchange._raw_stream(
            ["!forceOrder@arr"], emit_reconnect=True
        ):
            orders = data if isinstance(data, list) else [data]
            for item in orders:
                order = item.get("o", item)
                symbol = str(order.get("s", "")).upper()
                if allowed and symbol not in allowed:
                    continue
                event_time = _dt(item.get("E", order.get("T")))
                payload = {
                    "symbol": symbol,
                    "side": order.get("S"),
                    "order_type": order.get("o"),
                    "time_in_force": order.get("f"),
                    "quantity": float(order.get("q", 0.0)),
                    "price": float(order.get("p", 0.0)),
                    "average_price": float(order.get("ap", 0.0)),
                    "status": order.get("X"),
                    "event_time": order.get("T"),
                }
                yield _event(MarketEventType.LIQUIDATION, symbol, payload, event_time)

    async def stream_open_interest(
        self, symbols: list[str]
    ) -> AsyncIterator[MarketEvent]:
        bounded = list(dict.fromkeys(s.upper() for s in symbols if s))[
            :TOP_SYMBOL_LIMIT
        ]
        while True:
            for symbol in bounded:
                try:
                    oi = await self.exchange.fetch_open_interest(symbol)
                    yield _event(
                        MarketEventType.OPEN_INTEREST,
                        oi.symbol,
                        {
                            "symbol": oi.symbol,
                            "open_interest": oi.open_interest,
                            "timestamp": oi.timestamp.isoformat(),
                        },
                        oi.timestamp,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue
            await asyncio.sleep(OPEN_INTEREST_POLL_SECONDS)

    async def stream_instruments(
        self, symbols: list[str]
    ) -> AsyncIterator[MarketEvent]:
        requested = list(dict.fromkeys(s.upper() for s in symbols if s))
        while True:
            try:
                filters = await self.exchange.fetch_exchange_info(requested or None)
                now = datetime.now(timezone.utc)
                for symbol, value in filters.items():
                    payload = {
                        "symbol": symbol,
                        "tick_size": value.tick_size,
                        "step_size": value.step_size,
                        "min_notional": value.min_notional,
                        "quantity_precision": value.quantity_precision,
                        "price_precision": value.price_precision,
                    }
                    yield _event(MarketEventType.INSTRUMENT, symbol, payload, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(INSTRUMENT_POLL_SECONDS)
