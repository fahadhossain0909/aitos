import pytest
from aitos.capital_intelligence.runtime import RedisCheckpointStore

class FakeRedis:
    def __init__(self): self.data={}
    async def set(self,key,value,nx=False,px=None):
        if nx and key in self.data: return False
        self.data[key]=value; return True
    async def get(self,key): return self.data.get(key)
    async def eval(self,script,nkeys,*args):
        if "PEXPIRE" in script:
            return self.data.get(args[0])==args[1]
        if "DEL" in script:
            if self.data.get(args[0])==args[1]: self.data.pop(args[0]); return 1
            return 0
        if self.data.get(args[0])!=args[2]: return 0
        self.data[args[1]]=args[3]; return 1

@pytest.mark.asyncio
async def test_fenced_cursor_rejects_old_worker():
    store=RedisCheckpointStore(FakeRedis())
    old=await store.acquire("binance")
    assert old is not None
    await store.release(old)
    new=await store.acquire("binance")
    assert new is not None
    with pytest.raises(RuntimeError,match="lease lost"):
        await store.save_cursor("binance",old,"stale")
    await store.save_cursor("binance",new,"fresh")
    assert await store.load_cursor("binance")=="fresh"

@pytest.mark.asyncio
async def test_only_one_worker_acquires_source_lease():
    store=RedisCheckpointStore(FakeRedis())
    assert await store.acquire("evm") is not None
    assert await store.acquire("evm") is None
