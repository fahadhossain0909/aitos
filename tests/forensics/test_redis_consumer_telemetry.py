from __future__ import annotations

import pytest


class FakeRedis:
    async def xinfo_groups(self, stream):
        return [
            {
                b"name": b"scanner",
                b"pending": 3,
                b"lag": 7,
                b"entries-read": 100,
                b"last-delivered-id": b"1-0",
            }
        ]

    async def xinfo_consumers(self, stream, group):
        return [{b"name": b"scanner-123", b"pending": 3, b"idle": 12000}]


class FakeStatus:
    details = {}


@pytest.mark.asyncio
async def test_consumer_telemetry_exposes_group_and_consumer_inventory():
    from aitos.forensics.redis_consumer_telemetry import install

    class EventBus:
        _known_topics = {"market.trade"}
        _redis = FakeRedis()

        async def health_check(self):
            return FakeStatus()

    install(EventBus)
    status = await EventBus().health_check()
    groups = status.details["consumer_forensics"]["groups"]
    assert groups[0]["group"] == "scanner"
    assert groups[0]["pending"] == 3
    assert groups[0]["lag"] == 7
    assert groups[0]["consumers"][0]["name"] == "scanner-123"
    assert groups[0]["consumers"][0]["idle_ms"] == 12000
