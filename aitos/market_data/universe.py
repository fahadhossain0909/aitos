"""Market-universe resolution for venue-neutral runtime entrypoints."""

from __future__ import annotations

from aitos.exchange.base import ExchangeAdapter


async def resolve_live_universe(exchange: ExchangeAdapter) -> list[str]:
    """Return the currently exposed Binance USDT-M symbol universe.

    The production runtime must not carry a small hand-maintained symbol list.
    ``fetch_exchange_info`` is the adapter-neutral discovery boundary; symbols
    are normalized and restricted to USDT-quoted instruments. The canonical
    market-data runtime then owns the live stream fan-out.
    """
    filters = await exchange.fetch_exchange_info()
    symbols = sorted(
        {
            str(symbol).upper()
            for symbol in filters
            if str(symbol).upper().endswith("USDT")
        }
    )
    if not symbols:
        raise RuntimeError("exchange returned no USDT trading symbols")
    return symbols
