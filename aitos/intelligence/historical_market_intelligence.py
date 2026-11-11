"""On-demand historical evidence with bounded cache and shared Redis budget."""
from __future__ import annotations
import asyncio, hashlib, json, time, uuid
from datetime import datetime, timezone
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from aitos.intelligence.historical_analogue import search_historical_analogues
from aitos.models.market import Kline

_WEIGHT_LUA = """
local k=KEYS[1]; local now=tonumber(ARGV[1]); local window=tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE',k,0,now-window)
local rows=redis.call('ZRANGE',k,0,-1); local used=0
for _,v in ipairs(rows) do local w=string.match(v,'|(%d+)$'); if w then used=used+tonumber(w) end end
local weight=tonumber(ARGV[4]); if used+weight>tonumber(ARGV[5]) then return 0 end
redis.call('ZADD',k,now,ARGV[3]..'|'..weight); redis.call('PEXPIRE',k,window+5000); return 1
"""
_RELEASE_LUA = "if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0"

@dataclass(frozen=True, slots=True)
class HistoricalIntelligenceConfig:
    cache_ttl_seconds:int=900
    max_cache_entries:int=24
    max_cached_candles:int=12000
    max_candles_per_request:int=1000
    max_request_weight_per_minute:int=60
    max_concurrency:int=2
    analogue_window:int=20
    analogue_horizon:int=12
    analogue_top_k:int=8
    def __post_init__(self):
        if min(self.cache_ttl_seconds,self.max_cache_entries,self.max_cached_candles,self.max_candles_per_request,self.max_request_weight_per_minute,self.max_concurrency,self.analogue_window,self.analogue_horizon,self.analogue_top_k)<1 or self.max_candles_per_request>1500:
            raise ValueError("invalid HMIE resource limits")

def kline_request_weight(limit:int)->int:
    if not 1<=limit<=1500: raise ValueError("kline limit must be 1..1500")
    return 1 if limit<100 else 2 if limit<500 else 5 if limit<=1000 else 10

class HistoricalEvidenceStore:
    """Durable, append-only selected-candidate evidence in ClickHouse."""
    def __init__(self,client:Any,database:str="aitos"): self.client,self.database=client,database
    async def ensure_schema(self):
        await self.client.command(f"""CREATE TABLE IF NOT EXISTS {self.database}.historical_decision_evidence (
        evidence_id String,candidate_id String,symbol LowCardinality(String),timeframe LowCardinality(String),
        captured_at DateTime64(3,'UTC'),evidence_json String,schema_version UInt16,
        ingested_at DateTime64(3,'UTC') DEFAULT now64(3))
        ENGINE=ReplacingMergeTree(ingested_at) PARTITION BY toYYYYMM(captured_at)
        ORDER BY (candidate_id,evidence_id)""")
    async def append(self,candidate_id:str,evidence:dict[str,Any])->str:
        if not candidate_id: raise ValueError("candidate_id required")
        body=json.dumps(evidence,sort_keys=True,separators=(",",":"),allow_nan=False)
        eid=hashlib.sha256(f"{candidate_id}:{body}".encode()).hexdigest()
        await self.client.insert(f"{self.database}.historical_decision_evidence",
          [[eid,candidate_id,evidence["symbol"],evidence["timeframe"],evidence["captured_at"],body,1]],
          column_names=["evidence_id","candidate_id","symbol","timeframe","captured_at","evidence_json","schema_version"])
        return eid

class OnDemandHistoricalIntelligence:
    def __init__(self,fetcher:Callable[...,Awaitable[list[Kline]]],*,redis_client:Any=None,evidence_store:HistoricalEvidenceStore|None=None,config:HistoricalIntelligenceConfig|None=None):
        self.fetcher,self.redis,self.evidence_store=fetcher,redis_client,evidence_store
        self.config=config or HistoricalIntelligenceConfig()
        self._cache:OrderedDict[tuple[str,str,int],tuple[float,tuple[Kline,...]]]=OrderedDict()
        self._inflight:dict[tuple[str,str,int],asyncio.Task]= {}
        self._lock=asyncio.Lock(); self._semaphore=asyncio.Semaphore(self.config.max_concurrency)
        self._weights:list[tuple[float,int]]=[]; self._cached_candles=0
        self.metrics={"cache_hits":0,"cache_misses":0,"coalesced_requests":0,"budget_rejections":0,"fetch_errors":0,"fetched_candles":0,"distributed_cache_hits":0}
    def _local_budget(self,w:int)->bool:
        now=time.monotonic(); self._weights=[x for x in self._weights if now-x[0]<60]
        if sum(x[1] for x in self._weights)+w>self.config.max_request_weight_per_minute:return False
        self._weights.append((now,w)); return True
    async def _budget(self,w:int)->bool:
        if self.redis is None:return self._local_budget(w)
        try:return bool(await self.redis.eval(_WEIGHT_LUA,1,"aitos:hmie:request-weight:v1",int(time.time()*1000),60000,uuid.uuid4().hex,w,self.config.max_request_weight_per_minute))
        except Exception:self.metrics["budget_rejections"]+=1; return False
    async def _load(self,key:tuple[str,str,int])->list[Kline]|None:
        symbol,tf,limit=key; ck=f"aitos:hmie:klines:v1:{symbol}:{tf}:{limit}"; lk=f"aitos:hmie:lock:v1:{symbol}:{tf}:{limit}"
        token=uuid.uuid4().hex; locked=False
        if self.redis is not None:
            try:
                raw=await self.redis.get(ck)
                if raw:
                    if isinstance(raw,bytes):raw=raw.decode()
                    self.metrics["distributed_cache_hits"]+=1
                    return [Kline.from_dict(x) for x in json.loads(raw)]
                locked=bool(await self.redis.set(lk,token,nx=True,ex=20))
                if not locked:
                    for _ in range(20):
                        await asyncio.sleep(.1); raw=await self.redis.get(ck)
                        if raw:
                            if isinstance(raw,bytes):raw=raw.decode()
                            return [Kline.from_dict(x) for x in json.loads(raw)]
                    return None
            except Exception:self.metrics["fetch_errors"]+=1; return None
        try:
            if not await self._budget(kline_request_weight(limit)):
                self.metrics["budget_rejections"]+=1; return None
            async with self._semaphore: rows=await self.fetcher(symbol,tf,limit=limit)
            rows=sorted((r for r in rows if getattr(r,"is_closed",True)),key=lambda r:r.open_time)[-limit:]
            if not rows:return None
            if self.redis is not None:
                await self.redis.setex(ck,self.config.cache_ttl_seconds,json.dumps([r.to_dict() for r in rows],separators=(",",":")))
            self.metrics["fetched_candles"]+=len(rows); return rows
        except asyncio.CancelledError:raise
        except Exception:self.metrics["fetch_errors"]+=1; return None
        finally:
            if locked:
                try:await self.redis.eval(_RELEASE_LUA,1,lk,token)
                except Exception:pass
    async def get_klines(self,symbol:str,timeframe:str,*,limit:int=500)->list[Kline]|None:
        limit=min(int(limit),self.config.max_candles_per_request)
        if limit<1:return None
        key=(symbol.upper(),timeframe.lower(),limit); now=time.monotonic()
        async with self._lock:
            item=self._cache.get(key)
            if item and now-item[0]<self.config.cache_ttl_seconds:
                self._cache.move_to_end(key); self.metrics["cache_hits"]+=1; return list(item[1])
            task=self._inflight.get(key)
            if task:self.metrics["coalesced_requests"]+=1
            else:
                self.metrics["cache_misses"]+=1; task=asyncio.create_task(self._load(key)); self._inflight[key]=task
        try:
            rows=await task
            if rows:
                frozen=tuple(rows)
                async with self._lock:
                    old=self._cache.pop(key,None)
                    if old:self._cached_candles-=len(old[1])
                    self._cache[key]=(time.monotonic(),frozen); self._cached_candles+=len(frozen)
                    while len(self._cache)>self.config.max_cache_entries or self._cached_candles>self.config.max_cached_candles:
                        _,(_,evicted)=self._cache.popitem(last=False); self._cached_candles-=len(evicted)
                return list(frozen)
            return None
        finally:
            async with self._lock:
                if self._inflight.get(key) is task and task.done():self._inflight.pop(key,None)
    def build_context(self,symbol:str,timeframe:str,rows:list[Kline])->dict[str,Any]:
        w,h=self.config.analogue_window,self.config.analogue_horizon
        if len(rows)<2*w+h+1:return {"available":False,"reason":"insufficient_history","symbol":symbol,"timeframe":timeframe,"sample_count":len(rows),"evidence_only":True}
        matches=search_historical_analogues(rows,window=w,search_back=min(500,len(rows)),top_k=self.config.analogue_top_k,forward_horizon=h)
        outcome=matches[0].outcome if matches else None
        return {"available":bool(matches),"reason":None if matches else "no_valid_analogues","symbol":symbol.upper(),"timeframe":timeframe,"captured_at":rows[-1].close_time.isoformat(),"sample_count":len(rows),"analogue_count":len(matches),"analogue_indices":[m.start_index for m in matches],"analogue_similarities":[round(m.similarity,6) for m in matches],"up_probability":outcome.up_probability if outcome else None,"down_probability":outcome.down_probability if outcome else None,"flat_probability":outcome.flat_probability if outcome else None,"median_forward_return":outcome.median_return if outcome else None,"best_forward_return":outcome.best_return if outcome else None,"worst_forward_return":outcome.worst_return if outcome else None,"evidence_only":True,"calibration_status":"uncalibrated_empirical","lookahead_policy":"matched windows and complete forward horizons end before the current window"}
    async def analyze(self,symbol:str,timeframe:str,*,limit:int=500):
        rows=await self.get_klines(symbol,timeframe,limit=limit)
        if rows is None:return None,{"available":False,"reason":"historical_fetch_unavailable","symbol":symbol.upper(),"timeframe":timeframe,"evidence_only":True}
        return rows,self.build_context(symbol,timeframe,rows)
    async def persist(self,candidate_id:str,evidence:dict[str,Any])->str|None:
        if self.evidence_store is None:return None
        snapshot=dict(evidence)
        snapshot.setdefault("captured_at",datetime.now(timezone.utc).isoformat())
        return await self.evidence_store.append(candidate_id,snapshot)
