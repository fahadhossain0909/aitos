"""Authoritative, exact-match instrument master for market-data projections.

This registry intentionally does not infer asset class, venue, market type,
contract metadata, or aliases. A feed event is resolvable only when its
(normalized venue, market type, symbol) tuple has one explicitly registered
Instrument. Registry snapshots are immutable and safe to share with shadow
consumers; updates require constructing and swapping a new snapshot.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from aitos.market_data.contracts import MarketEvent as CanonicalMarketDataEvent

from .contracts import Instrument


def _part(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip().upper() if field == "symbol" else value.strip().lower()


@dataclass(frozen=True, slots=True)
class InstrumentKey:
    venue: str
    market_type: str
    symbol: str

    @classmethod
    def from_values(cls, venue: object, market_type: object, symbol: object):
        return cls(
            venue=_part(venue, "venue"),
            market_type=_part(market_type, "market_type"),
            symbol=_part(symbol, "symbol"),
        )


class InstrumentMaster:
    """Immutable exact-identity registry and MarketDataShadowConsumer resolver."""

    def __init__(self, instruments: Iterable[Instrument], *, version: str):
        if not isinstance(version, str) or not version.strip():
            raise ValueError("instrument master version must be explicit")
        entries: dict[InstrumentKey, Instrument] = {}
        ids: set[str] = set()
        for instrument in instruments:
            if not isinstance(instrument, Instrument):
                raise TypeError("instrument master accepts only Instrument values")
            if not instrument.market_type:
                raise ValueError("registered instruments require market_type")
            key = InstrumentKey.from_values(
                instrument.venue, instrument.market_type, instrument.symbol
            )
            if key in entries:
                raise ValueError(f"duplicate instrument identity: {key}")
            if instrument.id in ids:
                raise ValueError(f"duplicate instrument id: {instrument.id}")
            entries[key] = instrument
            ids.add(instrument.id)
        self._entries: Mapping[InstrumentKey, Instrument] = MappingProxyType(entries)
        self.version = version.strip()

    def resolve(
        self, *, venue: str, market_type: str, symbol: str
    ) -> Instrument | None:
        """Return an exact registered instrument; never guess or alias."""
        key = InstrumentKey.from_values(venue, market_type, symbol)
        return self._entries.get(key)

    def resolve_event(self, event: CanonicalMarketDataEvent) -> Instrument | None:
        """Resolve only canonical event identity fields, rejecting ambiguity."""
        if not isinstance(event, CanonicalMarketDataEvent):
            raise TypeError("canonical market-data event required")
        venue = event.venue or event.exchange
        market_type = event.market_type or event.market
        return self.resolve(
            venue=venue, market_type=market_type, symbol=event.symbol
        )

    def __call__(self, event: CanonicalMarketDataEvent) -> Instrument | None:
        """Callable adapter for MarketDataShadowConsumer.instrument_resolver."""
        return self.resolve_event(event)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def instrument_ids(self) -> tuple[str, ...]:
        return tuple(sorted(instrument.id for instrument in self._entries.values()))
