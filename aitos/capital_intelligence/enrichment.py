"""Token-unit normalization and point-in-time price enrichment."""

from __future__ import annotations

from dataclasses import replace
from datetime import timezone
from decimal import Decimal
from math import isfinite
from typing import Any

from .contracts import CapitalEvent
from .token_registry import TokenRegistry
from .valuation import PointInTimePriceBook


async def enrich_capital_event(
    event: CapitalEvent,
    *,
    registry: TokenRegistry,
    price_book: PointInTimePriceBook,
    max_quote_age_seconds: float = 120.0,
    allow_usdt_usd_proxy: bool = False,
) -> CapitalEvent:
    """Normalize raw units and attach a no-lookahead quote with full provenance.

    USDT is not silently treated as USD. Enabling the proxy is an explicit operator
    choice and is recorded in metadata; otherwise usd_value remains unset.
    """
    if not isfinite(max_quote_age_seconds) or max_quote_age_seconds <= 0:
        raise ValueError("max quote age must be positive and finite")
    chain_id = str(event.metadata.get("chain_id", event.venue))
    asset_address = str(event.metadata.get("asset_address", event.instrument))
    token = registry.get(chain_id, asset_address)
    if token is None:
        return replace(
            event,
            metadata={
                **dict(event.metadata),
                "valuation_status": "token_metadata_missing",
            },
        )

    raw = event.metadata.get("raw_amount")
    if raw is None:
        raw = event.metadata.get("raw_delta")
    if raw is None:
        raw = str(int(event.quantity))
    normalized = token.normalize_raw(str(raw).lstrip("-"))
    quantity = float(normalized)
    metadata: dict[str, Any] = {
        **dict(event.metadata),
        "asset_id": token.asset_id,
        "token_symbol": token.symbol,
        "token_decimals": token.decimals,
        "normalized_quantity_exact": format(normalized, "f"),
        "token_metadata_source": "operator_configured_registry",
    }
    quote = await price_book.quote(token.asset_id, event.event_time)
    if quote is None:
        metadata["valuation_status"] = "point_in_time_quote_missing"
        return replace(
            event,
            quantity=quantity,
            unit=token.symbol,
            usd_value=None,
            metadata=metadata,
        )

    event_time = event.event_time.astimezone(timezone.utc)
    quote_time = quote.observed_at.astimezone(timezone.utc)
    age = (event_time - quote_time).total_seconds()
    if age < 0 or age > max_quote_age_seconds:
        metadata["valuation_status"] = "quote_future_or_stale"
        metadata["quote_observed_at"] = quote_time.isoformat()
        return replace(event, quantity=quantity, unit=token.symbol, usd_value=None, metadata=metadata)

    quote_value = normalized * Decimal(str(quote.price))
    if not quote_value.is_finite():
        metadata["valuation_status"] = "invalid_quote_value"
        return replace(event, quantity=quantity, unit=token.symbol, usd_value=None, metadata=metadata)

    metadata.update(
        {
            "valuation_status": "valued",
            "quote_currency": quote.quote_currency.upper(),
            "quote_value": format(quote_value, "f"),
            "quote_source": quote.source,
            "quote_observed_at": quote_time.isoformat(),
        }
    )
    usd_value = None
    if quote.quote_currency.upper() == "USD":
        usd_value = float(quote_value)
        metadata["usd_valuation_basis"] = "USD quote"
    elif quote.quote_currency.upper() == "USDT" and allow_usdt_usd_proxy:
        usd_value = float(quote_value)
        metadata["usd_valuation_basis"] = (
            "USDT proxy; not an independent USD conversion"
        )
    else:
        metadata["usd_valuation_basis"] = "non-USD quote; usd_value intentionally unset"
    return replace(
        event,
        quantity=quantity,
        unit=token.symbol,
        usd_value=usd_value,
        metadata=metadata,
    )
