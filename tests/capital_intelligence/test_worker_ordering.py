from datetime import datetime, timezone
import pytest

from aitos.capital_intelligence.contracts import CapitalEvent
from aitos.capital_intelligence.runtime import (
    CapitalIngestionWorker,
    RedisCheckpointStore,
)


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def set(self, key, value, nx=False, px=None):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def get(self, key):
        return self.data.get(key)

    async def eval(self, script, number_of_keys, *args):
        if "PEXPIRE" in script:
            return int(self.data.get(args[0]) == args[1])
        if "DEL" in script:
            if self.data.get(args[0]) == args[1]:
                del self.data[args[0]]
                return 1
            return 0
        if self.data.get(args[0]) != args[2]:
            return 0
        self.data[args[1]] = args[3]
        return 1


class Source:
    async def poll(self, *, cursor=None):
        now = datetime.now(timezone.utc)
        event = CapitalEvent(
            "event-1",
            "source-1",
            "test",
            "crypto",
            "test",
            "BTC",
            "test_event",
            now,
            now,
            1,
            "BTC",
            None,
        )
        return [event], "next"

    async def close(self):
        return None


class Store:
    def __init__(self, checkpoints):
        self.checkpoints = checkpoints
        self.appended = False

    async def append(self, events):
        self.appended = True


@pytest.mark.asyncio
async def test_worker_persists_events_before_checkpoint():
    redis_client = FakeRedis()
    checkpoints = RedisCheckpointStore(redis_client)
    store = Store(checkpoints)
    worker = CapitalIngestionWorker(
        source_id="source-1",
        source=Source(),
        checkpoints=checkpoints,
        store=store,
    )
    assert await worker.run_once() == 1
    assert store.appended
    assert await checkpoints.load_cursor("source-1") == "next"


@pytest.mark.asyncio
async def test_reorg_tombstones_precede_new_events_and_cursor_checkpoint():
    order = []

    class OrderedRedis(FakeRedis):
        async def eval(self, script, number_of_keys, *args):
            if "PEXPIRE" not in script and "DEL" not in script:
                order.append("checkpoint")
            return await super().eval(script, number_of_keys, *args)

    class ReorgSource(Source):
        last_reorg = {
            "source_id": "evm:1",
            "from_block": 10,
            "to_block": 10,
            "canonical_hashes": {10: "0xcanonical"},
        }

    class ReorgStore(Store):
        async def reconcile_evm_reorg(self, instruction):
            order.append("reorg")

        async def append(self, events):
            order.append("append")
            self.appended = True

    redis_client = OrderedRedis()
    checkpoints = RedisCheckpointStore(redis_client)
    store = ReorgStore(checkpoints)
    worker = CapitalIngestionWorker(
        source_id="evm-source",
        source=ReorgSource(),
        checkpoints=checkpoints,
        store=store,
    )
    assert await worker.run_once() == 1
    assert order.index("reorg") < order.index("append") < order.index("checkpoint")
