"""Convert exchange-specific rows into the canonical event schema."""

from __future__ import annotations

from typing import Any

from .schema import CanonicalTrade, normalize_timestamp


def normalize_binance_aggtrade(
    row: dict[str, Any], symbol: str, market: str = "futures_um"
) -> CanonicalTrade:
    """Normalize a Binance aggTrade row represented as a mapping."""
    return CanonicalTrade(
        exchange="binance",
        market=market,
        symbol=symbol.upper(),
        trade_id=str(row.get("a", row.get("trade_id"))),
        timestamp=normalize_timestamp(row["timestamp"]),
        price=float(row["p"] if "p" in row else row["price"]),
        quantity=float(row["q"] if "q" in row else row["quantity"]),
        side="sell" if bool(row.get("m", row.get("is_buyer_maker", False))) else "buy",
        is_buyer_maker=bool(row.get("m", row.get("is_buyer_maker", False))),
    )


def normalize_bybit_trade(
    row: dict[str, Any], symbol: str, market: str = "spot"
) -> CanonicalTrade:
    """Normalize a Bybit trade row represented as a mapping."""
    side = str(row.get("side", "")).lower()
    if side not in {"buy", "sell"}:
        raise ValueError(f"Unsupported Bybit trade side: {side!r}")
    return CanonicalTrade(
        exchange="bybit",
        market=market,
        symbol=symbol.upper(),
        trade_id=str(row.get("execId", row.get("trade_id", ""))),
        timestamp=normalize_timestamp(row["timestamp"]),
        price=float(row.get("price")),
        quantity=float(row.get("qty", row.get("quantity"))),
        side=side,  # type: ignore[arg-type]
        is_buyer_maker=None,
    )
