"""Venue-neutral market-data identity and capability definitions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Venue(str, Enum):
    BINANCE = "binance"
    BYBIT = "bybit"
    OKX = "okx"
    KUCOIN = "kucoin"
    HYPERLIQUID = "hyperliquid"
    UNISWAP = "uniswap"
    JUPITER = "jupiter"


class MarketType(str, Enum):
    SPOT = "spot"
    USD_M_FUTURES = "usd_m_futures"
    COIN_M_FUTURES = "coin_m_futures"
    OPTIONS = "options"
    PERPETUAL = "perpetual"
    DEX_POOL = "dex_pool"


@dataclass(frozen=True, slots=True)
class InstrumentKey:
    """Stable internal identity for a venue instrument or DEX pool."""

    venue: Venue
    market_type: MarketType
    symbol: str

    @property
    def value(self) -> str:
        return f"{self.venue.value}:{self.market_type.value}:{self.symbol}"


@dataclass(frozen=True, slots=True)
class VenueCapabilities:
    """Market-data capabilities advertised by a venue adapter."""

    trades: bool = True
    order_book: bool = True
    ticker: bool = True
    klines: bool = True
    funding: bool = False
    open_interest: bool = False
    liquidations: bool = False
    options: bool = False
    instruments: bool = True
    rest_recovery: bool = True


@dataclass(frozen=True, slots=True)
class VenueConfig:
    """Configuration shared by all venue adapters."""

    venue: Venue
    market_types: tuple[MarketType, ...]
    capabilities: VenueCapabilities = VenueCapabilities()
    enabled: bool = True

    def supports(self, market_type: MarketType) -> bool:
        return market_type in self.market_types


DEFAULT_VENUES: tuple[VenueConfig, ...] = (
    VenueConfig(
        Venue.BINANCE,
        (
            MarketType.SPOT,
            MarketType.USD_M_FUTURES,
            MarketType.COIN_M_FUTURES,
            MarketType.OPTIONS,
        ),
        VenueCapabilities(
            funding=True, open_interest=True, liquidations=True, options=True
        ),
    ),
    VenueConfig(
        Venue.BYBIT,
        (
            MarketType.SPOT,
            MarketType.USD_M_FUTURES,
            MarketType.COIN_M_FUTURES,
            MarketType.OPTIONS,
        ),
        VenueCapabilities(
            funding=True, open_interest=True, liquidations=True, options=True
        ),
    ),
    VenueConfig(
        Venue.OKX,
        (
            MarketType.SPOT,
            MarketType.USD_M_FUTURES,
            MarketType.COIN_M_FUTURES,
            MarketType.OPTIONS,
        ),
        VenueCapabilities(
            funding=True, open_interest=True, liquidations=True, options=True
        ),
    ),
    VenueConfig(
        Venue.KUCOIN,
        (MarketType.SPOT, MarketType.USD_M_FUTURES),
        VenueCapabilities(funding=True, open_interest=True, liquidations=True),
    ),
    VenueConfig(
        Venue.HYPERLIQUID,
        (MarketType.PERPETUAL, MarketType.SPOT),
        VenueCapabilities(funding=True, open_interest=True, liquidations=True),
    ),
    VenueConfig(
        Venue.UNISWAP,
        (MarketType.DEX_POOL,),
        VenueCapabilities(
            order_book=False, funding=False, open_interest=False, liquidations=False
        ),
    ),
    VenueConfig(
        Venue.JUPITER,
        (MarketType.DEX_POOL,),
        VenueCapabilities(
            order_book=False, funding=False, open_interest=False, liquidations=False
        ),
    ),
)
