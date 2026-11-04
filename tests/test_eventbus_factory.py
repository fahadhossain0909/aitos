"""Backend selection tests; volatile transport must never be used for trading."""

import pytest

from aitos.eventbus.factory import create_event_bus
from aitos.eventbus.inprocess_bus import InProcessBus
from aitos.eventbus.redis_bus import EventBus as RedisEventBus


def test_redis_is_the_default_backend_and_requires_client() -> None:
    with pytest.raises(ValueError, match="redis_client is required"):
        create_event_bus()
    bus = create_event_bus(redis_client=object())
    assert isinstance(bus, RedisEventBus)


def test_factory_creates_inprocess_for_development() -> None:
    bus = create_event_bus({"backend": "inprocess", "runtime_mode": "development"})
    assert isinstance(bus, InProcessBus)


@pytest.mark.parametrize("mode", ["paper", "live", "production", "prod", "paper-trading", "live-trading", "staging", "unknown", ""])
def test_factory_rejects_volatile_backend_for_trading_modes(mode: str) -> None:
    with pytest.raises(ValueError, match="only allowed"):
        create_event_bus({"backend": "inprocess", "runtime_mode": mode})


def test_factory_does_not_silently_select_unimplemented_nats() -> None:
    with pytest.raises(NotImplementedError, match="NATS JetStream"):
        create_event_bus({"backend": "nats"})


def test_factory_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="Unsupported EventBus backend"):
        create_event_bus({"backend": "unknown"})
