from datetime import datetime, timezone
import pytest

from aitos.capital_intelligence.contracts import CapitalEvent


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
