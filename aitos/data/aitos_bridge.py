"""Bridge canonical historical events into AITOS domain models."""
from __future__ import annotations

from aitos.data.schema import CanonicalBookEvent, CanonicalTrade
from aitos.models.market import OrderBookSnapshot, TradeSide, TradeTick


def canonical_trade_to_domain(event: CanonicalTrade) -> TradeTick:
    """Convert the exchange-neutral trade schema to the existing TradeTick model."""
    try:
        trade_id = int(event.trade_id)
    except (TypeError, ValueError):
        trade_id = abs(hash((event.exchange, event.market, event.symbol, event.trade_id)))
    return TradeTick(
        symbol=event.symbol,
        trade_id=trade_id,
        price=float(event.price),
        quantity=float(event.quantity),
        side=TradeSide(event.side),
        is_buyer_maker=bool(event.is_buyer_maker) if event.is_buyer_maker is not None else event.side == "sell",
        timestamp=event.timestamp,
    )


def book_events_to_snapshot(events: list[CanonicalBookEvent]) -> OrderBookSnapshot | None:
    """Apply a batch of absolute L2 updates and emit the existing snapshot model."""
    if not events:
        return None
    bids: dict[float, float] = {}
    asks: dict[float, float] = {}
    latest_id = events[-1].update_id
    for event in events:
        book = bids if event.side == "buy" else asks
        price = float(event.price)
        qty = float(event.quantity)
        if qty <= 0:
            book.pop(price, None)
        else:
            book[price] = qty
    try:
        update_id = int(latest_id)
    except (TypeError, ValueError):
        update_id = 0
    return OrderBookSnapshot(
        symbol=events[0].symbol,
        bids=tuple(sorted(bids.items(), reverse=True)),
        asks=tuple(sorted(asks.items())),
        last_update_id=update_id,
        timestamp=events[-1].timestamp,
    )
