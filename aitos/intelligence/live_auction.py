"""Live Auction Market Theory context from executed trades and L2 state.

This is intentionally a proxy, not a true historical volume profile. It uses
recent executed prices/volume plus the current visible book to estimate
acceptance, balance and directional extension.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from aitos.models.market import OrderBookSnapshot, TradeTick


def live_auction_score(
    trades: Iterable[TradeTick], book: OrderBookSnapshot | None, direction: str
) -> float:
    if direction not in {"long", "short"}:
        return 5.0
    ticks = list(trades)
    if len(ticks) < 5:
        return 5.0
    prices = [t.price for t in ticks]
    low, high = min(prices), max(prices)
    width = high - low
    if width <= 0:
        return 5.0
    last = ticks[-1].price
    location = (last - low) / width
    volume_by_price: dict[float, float] = defaultdict(float)
    for trade in ticks:
        volume_by_price[trade.price] += trade.quantity
    poc_price = max(volume_by_price, key=volume_by_price.get)
    poc_location = (poc_price - low) / width
    acceptance = max(0.0, 1.0 - abs(location - poc_location) * 2.0)
    score = (
        5.0 + (location - 0.5) * 4.0 + acceptance * 1.0
        if direction == "long"
        else 5.0 + (0.5 - location) * 4.0 + acceptance * 1.0
    )
    if book is not None:
        bid = sum(q for _, q in book.bids)
        ask = sum(q for _, q in book.asks)
        total = bid + ask
        if total > 0:
            book_bias = (bid - ask) / total
            score += book_bias * 1.0 if direction == "long" else -book_bias * 1.0
    return round(max(0.0, min(10.0, score)), 2)
