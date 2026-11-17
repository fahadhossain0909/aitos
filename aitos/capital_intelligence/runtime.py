"""Crash-safe, fenced ingestion orchestration for CFEI sources.

Delivery is at-least-once: persist events before advancing a source cursor.
Consumers must deduplicate by CapitalEvent.deduplication_key.
"""
from __future__ import annotations
import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Protocol
from .contracts import CapitalEvent

log=logging.getLogger(__name__)

class CapitalSource(Protocol):
    async def poll(self, *, cursor: str | None = None) -> tuple[list[CapitalEvent], str | None]: ...
    async def close(self) -> None: ...

@dataclass(frozen=True, slots=True)
class Lease:
    key: str
    token: str
    ttl_ms: int

class RedisCheckpointStore:
    """Redis lease + cursor; Lua scripts prevent stale workers committing."""
    _RENEW = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('PEXPIRE', KEYS[1], ARGV[2])
    end
    return 0
    """
    _RELEASE = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    end
    return 0
    """
    _SAVE = """
    if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
    redis.call('SET', KEYS[2], ARGV[2])
    return 1
    """
    def __init__(self, redis: Any, namespace: str = "aitos:cfei"):
        if not namespace or any(c.isspace() for c in namespace): raise ValueError("invalid namespace")
        self.redis=redis; self.namespace=namespace.rstrip(":")
    def _key(self, source_id: str) -> str:
        if not source_id or any(c.isspace() for c in source_id): raise ValueError("invalid source id")
        return f"{self.namespace}:{source_id}"
    async def acquire(self, source_id: str, ttl_ms: int = 30_000) -> Lease | None:
        if not 1000 <= ttl_ms <= 300_000: raise ValueError("lease ttl outside safe bounds")
        key=self._key(source_id)+":lease"; token=secrets.token_urlsafe(24)
        ok=await self.redis.set(key,token,nx=True,px=ttl_ms)
        return Lease(key,token,ttl_ms) if ok else None
    async def renew(self, lease: Lease) -> bool:
        return bool(await self.redis.eval(self._RENEW,1,lease.key,lease.token,lease.ttl_ms))
    async def release(self, lease: Lease) -> None:
        await self.redis.eval(self._RELEASE,1,lease.key,lease.token)
    async def load_cursor(self, source_id: str) -> str | None:
        value=await self.redis.get(self._key(source_id)+":cursor")
        if value is None: return None
        return value.decode() if isinstance(value,bytes) else str(value)
    async def save_cursor(self, source_id: str, lease: Lease, cursor: str) -> None:
        if not cursor or len(cursor)>4096: raise ValueError("invalid cursor")
        ok=await self.redis.eval(self._SAVE,2,lease.key,self._key(source_id)+":cursor",lease.token,cursor)
        if not ok: raise RuntimeError("lease lost; refusing stale cursor commit")

class CapitalIngestionWorker:
    def __init__(self, *, source_id: str, source: CapitalSource, checkpoints: RedisCheckpointStore, store: Any, poll_interval_seconds: float = 15.0, batch_limit: int = 10_000):
        if not 0.5 <= poll_interval_seconds <= 3600: raise ValueError("poll interval outside safe bounds")
        if not 1 <= batch_limit <= 50_000: raise ValueError("batch limit outside safe bounds")
        self.source_id=source_id; self.source=source; self.checkpoints=checkpoints; self.store=store
        self.poll_interval_seconds=poll_interval_seconds; self.batch_limit=batch_limit; self._stopping=asyncio.Event()
    def stop(self): self._stopping.set()
    async def run_once(self) -> int:
        lease=await self.checkpoints.acquire(self.source_id)
        if lease is None: return 0
        try:
            cursor=await self.checkpoints.load_cursor(self.source_id)
            events,next_cursor=await self.source.poll(cursor=cursor)
            if len(events)>self.batch_limit: raise RuntimeError("source batch exceeded configured limit")
            if events:
                await self.store.append(events)
            if next_cursor is not None and next_cursor != cursor:
                await self.checkpoints.save_cursor(self.source_id,lease,next_cursor)
            return len(events)
        finally:
            await self.checkpoints.release(lease)
    async def run_forever(self):
        while not self._stopping.is_set():
            started=time.monotonic()
            try: await self.run_once()
            except asyncio.CancelledError: raise
            except Exception:
                log.exception("CFEI source poll failed",extra={"source_id":self.source_id})
            delay=max(0.0,self.poll_interval_seconds-(time.monotonic()-started))
            try: await asyncio.wait_for(self._stopping.wait(),timeout=delay)
            except asyncio.TimeoutError: pass
    async def close(self):
        self.stop()
        await self.source.close()
