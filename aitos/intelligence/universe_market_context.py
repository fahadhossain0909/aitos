"""Compact all-market ticker context; no historical candles are retained."""
from __future__ import annotations
import json,math,time
from collections import OrderedDict
from typing import Any
from aitos.market_data.bus import MarketDataBus
from aitos.market_data.contracts import MarketEvent,MarketEventType
KEY="aitos:hmie:universe-context:v1"
class UniverseMarketContext:
    def __init__(self,redis_client:Any=None,*,ttl_seconds:int=30,max_symbols:int=2500):
        self.redis=redis_client; self.ttl_seconds=max(5,ttl_seconds); self.max_symbols=max(50,max_symbols)
        self._memory:OrderedDict[str,dict[str,Any]]=OrderedDict(); self._subscription=None
        self.metrics={"ticker_events":0,"redis_errors":0,"stale_symbols":0}
    async def start(self,bus:MarketDataBus):
        if self._subscription is None:self._subscription=await bus.subscribe(MarketEventType.TICKER,self.on_ticker,group="hmie-universe-context-v1",live_only=True)
    async def stop(self):
        if self._subscription:
            self._subscription.cancel()
            try:await self._subscription._task
            except BaseException:pass
            self._subscription=None
    async def on_ticker(self,event:MarketEvent):
        p=event.payload
        try:
            close=float(p.get("c",p.get("lastPrice",p.get("price",0)))); change=float(p.get("P",p.get("priceChangePercent",0)))
            qv=float(p.get("q",p.get("quoteVolume",0))); count=int(p.get("n",p.get("count",0)))
            high=float(p.get("h",p.get("highPrice",close))); low=float(p.get("l",p.get("lowPrice",close)))
            bid=float(p.get("b",p.get("bidPrice",0))); ask=float(p.get("a",p.get("askPrice",0)))
            if close<=0 or not all(math.isfinite(x) for x in (close,change,qv,high,low)):return
            row={"symbol":event.symbol.upper(),"timestamp":event.event_time.timestamp(),"last_price":close,"change_pct_24h":change,"quote_volume_24h":max(0,qv),"trade_count_24h":max(0,count),"range_pct_24h":max(0,(high-low)/close*100),"spread_pct":(ask-bid)/close*100 if ask>bid>0 else None}
        except (TypeError,ValueError,OverflowError):return
        self.metrics["ticker_events"]+=1; self._memory[event.symbol.upper()]=row; self._memory.move_to_end(event.symbol.upper())
        while len(self._memory)>self.max_symbols:self._memory.popitem(last=False)
        if self.redis is not None:
            try:await self.redis.hset(KEY,event.symbol.upper(),json.dumps(row,separators=(",",":"))); await self.redis.expire(KEY,self.ttl_seconds)
            except Exception:self.metrics["redis_errors"]+=1
    async def snapshot(self,*,max_age_seconds:int=20):
        now=time.time(); result={}
        if self.redis is not None:
            try:
                raw=await self.redis.hgetall(KEY)
                for s,v in raw.items():
                    if isinstance(s,bytes):s=s.decode()
                    if isinstance(v,bytes):v=v.decode()
                    row=json.loads(v)
                    if now-float(row["timestamp"])<=max_age_seconds:result[str(s).upper()]=row
            except Exception:self.metrics["redis_errors"]+=1
        if not result:result={s:r for s,r in self._memory.items() if now-float(r["timestamp"])<=max_age_seconds}
        self.metrics["stale_symbols"]=max(0,len(self._memory)-len(result)); return result
    @staticmethod
    def score(row):
        momentum=min(10,abs(float(row.get("change_pct_24h",0)))*1.5)
        liquidity=min(10,math.log10(max(1,float(row.get("quote_volume_24h",0))))*.75)
        activity=min(10,math.log10(max(1,float(row.get("trade_count_24h",0))))*1.5)
        spread=row.get("spread_pct"); spread_score=5 if spread is None else max(0,10-float(spread)*100)
        range_score=min(10,float(row.get("range_pct_24h",0))*.75)
        return round(.35*momentum+.30*liquidity+.15*activity+.10*spread_score+.10*range_score,6)
    async def rank(self):
        rows=await self.snapshot()
        return sorted(((s,self.score(r)) for s,r in rows.items()),key=lambda x:(-x[1],x[0]))
    def health_snapshot(self):return {**self.metrics,"memory_symbols":len(self._memory)}
