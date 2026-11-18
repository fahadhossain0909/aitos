import json

import pytest

from aitos.capital_intelligence.watchlist import RedisEntityWatchlist


class FakeRedis:
    def __init__(self):
        self.hashes = {}

    async def eval(self, script, key_count, key, entity_id, payload, maximum):
        values = self.hashes.setdefault(key, {})
        if entity_id not in values and len(values) >= int(maximum):
            return 0
        values[entity_id] = payload
        return 1

    async def hdel(self, key, entity_id):
        return int(self.hashes.get(key, {}).pop(entity_id, None) is not None)

    async def hgetall(self, key):
        return self.hashes.get(key, {})


@pytest.mark.asyncio
async def test_watchlist_add_is_idempotent_and_normalizes_evm_address():
    redis = FakeRedis()
    watchlist = RedisEntityWatchlist(redis)
    first = await watchlist.add("Ethereum", "0x" + "A" * 40, "treasury")
    second = await watchlist.add("ethereum", "0x" + "a" * 40, "updated note")
    assert first["entity_id"] == second["entity_id"]
    assert second["note"] == "updated note"
    rows = await watchlist.list()
    assert len(rows) == 1
    assert rows[0]["address"] == "0x" + "a" * 40


@pytest.mark.asyncio
async def test_watchlist_preserves_case_sensitive_base58_addresses():
    watchlist = RedisEntityWatchlist(FakeRedis())
    row = await watchlist.add("Solana", "AbCdEf123", "wallet")
    assert row["entity_id"] == "solana:AbCdEf123"


@pytest.mark.asyncio
async def test_watchlist_capacity_and_remove():
    watchlist = RedisEntityWatchlist(FakeRedis(), max_entries=1)
    await watchlist.add("ethereum", "0x" + "1" * 40)
    with pytest.raises(ValueError, match="capacity"):
        await watchlist.add("ethereum", "0x" + "2" * 40)
    assert await watchlist.remove("ethereum", "0x" + "1" * 40)
    assert not await watchlist.remove("ethereum", "0x" + "1" * 40)


@pytest.mark.asyncio
async def test_watchlist_rejects_invalid_input():
    watchlist = RedisEntityWatchlist(FakeRedis())
    with pytest.raises(ValueError):
        await watchlist.add("", "address")
    with pytest.raises(ValueError):
        await watchlist.add("ethereum", "address", "x" * 281)
    with pytest.raises(ValueError):
        await watchlist.list(limit=0)
