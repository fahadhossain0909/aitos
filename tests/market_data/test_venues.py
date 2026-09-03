import pytest

from aitos.market_data.registry import VenueRegistry
from aitos.market_data.venues import (
    DEFAULT_VENUES,
    InstrumentKey,
    MarketType,
    Venue,
    VenueConfig,
)


def test_instrument_key_is_stable_and_venue_scoped():
    key = InstrumentKey(Venue.BINANCE, MarketType.USD_M_FUTURES, "BTCUSDT")

    assert key.value == "binance:usd_m_futures:BTCUSDT"
    assert key != InstrumentKey(Venue.BYBIT, MarketType.USD_M_FUTURES, "BTCUSDT")


def test_default_registry_covers_required_venues():
    registry = VenueRegistry()

    assert registry.venues == tuple(config.venue for config in DEFAULT_VENUES)
    assert set(registry.venues) == {
        Venue.BINANCE,
        Venue.BYBIT,
        Venue.OKX,
        Venue.KUCOIN,
        Venue.HYPERLIQUID,
        Venue.UNISWAP,
        Venue.JUPITER,
    }


def test_registry_supports_expected_market_types():
    registry = VenueRegistry()

    assert registry.supports(Venue.BINANCE, MarketType.USD_M_FUTURES)
    assert registry.supports(Venue.BYBIT, MarketType.SPOT)
    assert registry.supports(Venue.OKX, MarketType.OPTIONS)
    assert registry.supports(Venue.KUCOIN, MarketType.USD_M_FUTURES)
    assert registry.supports(Venue.HYPERLIQUID, MarketType.PERPETUAL)
    assert registry.supports(Venue.UNISWAP, MarketType.DEX_POOL)
    assert registry.supports(Venue.JUPITER, MarketType.DEX_POOL)
    assert not registry.supports(Venue.UNISWAP, MarketType.USD_M_FUTURES)


def test_registry_rejects_unsupported_market_type():
    registry = VenueRegistry()

    with pytest.raises(ValueError, match="not supported"):
        registry.require_supported(Venue.UNISWAP, MarketType.SPOT)


def test_registry_rejects_duplicate_venues():
    config = VenueConfig(Venue.BINANCE, (MarketType.SPOT,))

    with pytest.raises(ValueError, match="duplicate venue configuration"):
        VenueRegistry((config, config))


def test_dex_capabilities_do_not_claim_order_book_derivatives():
    for venue in (Venue.UNISWAP, Venue.JUPITER):
        capabilities = VenueRegistry().get(venue).capabilities
        assert capabilities.order_book is False
        assert capabilities.funding is False
        assert capabilities.open_interest is False
        assert capabilities.liquidations is False
