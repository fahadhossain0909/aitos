import json
from datetime import datetime,timezone
import pytest
from aitos.intelligence.universe_market_context import UniverseMarketContext
from aitos.market_data.contracts import MarketEvent,MarketEventType,MarketSource

class FakeRedis:
 def __init__(self): self.data={}
 async def hset(self,key,field,value): self.data[field]=value
 async def expire(self,key,seconds): return True
 async def hgetall(self,key): return {k:v.encode() for k,v in self.data.items()}

@pytest.mark.asyncio
async def test_all_market_tickers_are_compacted_and_ranked_without_candles():
 redis=FakeRedis(); service=UniverseMarketContext(redis); now=datetime.now(timezone.utc)
 for symbol,pct,qv in [("BTCUSDT",2,1e9),("ETHUSDT",4,1e8),("XUSDT",1,1e5)]:
  event=MarketEvent(event_type=MarketEventType.TICKER,exchange="binance",venue="binance",market="usd_m_futures",market_type="futures",symbol=symbol,instrument_id=symbol,event_time=now,source=MarketSource.WEBSOCKET,payload={"c":"100","P":str(pct),"q":str(qv),"n":"1000","h":"105","l":"95","b":"99.9","a":"100.1"})
  await service.on_ticker(event)
 ranked=await service.rank()
 assert ranked[0][0]=="ETHUSDT"
 assert len(await service.snapshot())==3
 assert service.health_snapshot()["memory_symbols"]==3

def test_universe_score_is_bounded_and_finite():
 score=UniverseMarketContext.score({"change_pct_24h":-2,"quote_volume_24h":1e8,"trade_count_24h":1000,"range_pct_24h":5,"spread_pct":.01})
 assert 0<=score<=10
