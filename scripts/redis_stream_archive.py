#!/usr/bin/env python3
"""Crash-safe Redis Stream archival with durable per-stream cursors."""
from __future__ import annotations
import asyncio, json, os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import redis.asyncio as redis

HOST=os.getenv("REDIS_HOST","redis"); PORT=int(os.getenv("REDIS_PORT","6379"))
ROOT=Path(os.getenv("REDIS_ARCHIVE_DIR","/archive")); CURSOR_FILE=ROOT/".cursors.json"
POLL=max(.1,float(os.getenv("REDIS_ARCHIVE_POLL_SECONDS","1"))); BATCH=max(1,int(os.getenv("REDIS_ARCHIVE_BATCH_SIZE","1000")))
DEFAULT_MAXLEN=max(1,int(os.getenv("REDIS_STREAM_MAXLEN_DEFAULT","5000")))
STREAM_MAXLEN={"stream:market.trade.":25000,"stream:market.orderbook.":25000,"stream:market.liquidity.":100000,"stream:market.live_state.":25000,"stream:market.orderflow.":25000,"stream:market.kline.":10000,"stream:market.opportunity_scanned":5000,"stream:decision.":10000,"stream:journal.":10000,"stream:trade.":10000,"stream:risk.":10000,"stream:intel.":10000,"stream:dlq":25000}

def maxlen_for(key):
    for prefix,n in STREAM_MAXLEN.items():
        if key.startswith(prefix): return n
    return DEFAULT_MAXLEN

def safe_name(key): return key.removeprefix("stream:").replace("/","_").replace("..","_")
def dec(v:Any): return v.decode() if isinstance(v,bytes) else str(v)

class ArchiveWriter:
    def __init__(self): self.root=ROOT; self.root.mkdir(parents=True,exist_ok=True)
    def append(self,key,entries):
        d=self.root/safe_name(key); d.mkdir(parents=True,exist_ok=True)
        p=d/(datetime.now(timezone.utc).strftime("%Y-%m-%d")+".jsonl")
        with p.open("a",encoding="utf-8") as f:
            for eid,fields in entries:
                f.write(json.dumps({"stream":key,"stream_id":dec(eid),"fields":{dec(k):dec(v) for k,v in fields.items()}},ensure_ascii=False,separators=(",",":"))+"\n")
            f.flush(); os.fsync(f.fileno())
    def save(self,cursors):
        ROOT.mkdir(parents=True,exist_ok=True); tmp=CURSOR_FILE.with_suffix(".tmp")
        with tmp.open("w",encoding="utf-8") as f:
            json.dump(cursors,f,sort_keys=True,separators=(",",":")); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,CURSOR_FILE)
    def load(self):
        try:
            v=json.loads(CURSOR_FILE.read_text(encoding="utf-8")); return v if isinstance(v,dict) else {}
        except (FileNotFoundError,json.JSONDecodeError,OSError): return {}

async def archive_stream(r,writer,key,cursors):
    cur=cursors.get(key,"0-0")
    response=await r.xread({key:cur},count=BATCH)
    if response:
        _,entries=response[0]; writer.append(key,entries); cursors[key]=dec(entries[-1][0]); writer.save(cursors); return True
    maxlen=maxlen_for(key); newest=await r.xrevrange(key,max="+",min="-",count=maxlen+1)
    if len(newest)<=maxlen: return False
    boundary=dec(newest[-1][0])
    if cur < boundary: return False
    await r.xtrim(key,maxlen=maxlen,approximate=True); return True

async def main():
    writer=ArchiveWriter(); cursors=writer.load(); r=redis.Redis(host=HOST,port=PORT,decode_responses=False)
    try:
        await r.ping()
        while True:
            keys=[]
            async for raw in r.scan_iter(match="stream:*",count=200): keys.append(dec(raw))
            for key in sorted(keys):
                for _ in range(20):
                    if not await archive_stream(r,writer,key,cursors): break
            await asyncio.sleep(POLL)
    finally: await r.aclose()

if __name__=="__main__": asyncio.run(main())
