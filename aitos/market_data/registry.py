"""Registry for enabled canonical market-data venues."""

from __future__ import annotations

from collections.abc import Iterable

from .venues import DEFAULT_VENUES, MarketType, Venue, VenueConfig


class VenueRegistry:
    """Validate and resolve venue configuration without venue-specific logic."""

    def __init__(self, configs: Iterable[VenueConfig] = DEFAULT_VENUES) -> None:
        config_list = tuple(configs)
        by_venue: dict[Venue, VenueConfig] = {}
        for config in config_list:
            if config.venue in by_venue:
                raise ValueError(f"duplicate venue configuration: {config.venue.value}")
            by_venue[config.venue] = config
        self._configs = by_venue

    def get(self, venue: Venue) -> VenueConfig:
        """Return configuration for a venue, raising for unknown venues."""
        try:
            return self._configs[venue]
        except KeyError as exc:
            raise KeyError(f"unknown venue: {venue.value}") from exc

    def is_enabled(self, venue: Venue) -> bool:
        return self.get(venue).enabled

    def supports(self, venue: Venue, market_type: MarketType) -> bool:
        config = self.get(venue)
        return config.enabled and config.supports(market_type)

    def require_supported(self, venue: Venue, market_type: MarketType) -> None:
        """Raise a clear configuration error for an unsupported market type."""
        config = self.get(venue)
        if not config.enabled:
            raise ValueError(f"venue disabled: {venue.value}")
        if not config.supports(market_type):
            raise ValueError(
                f"market type {market_type.value} is not supported by {venue.value}"
            )

    @property
    def venues(self) -> tuple[Venue, ...]:
        """Return configured venues in deterministic insertion order."""
        return tuple(self._configs)
