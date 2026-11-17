"""Redis-backed per-source leases with monotonic fencing tokens."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)


_ACQUIRE = """
if redis.call('EXISTS', KEYS[1]) == 1 then return nil end
local fence = redis.call('INCR', KEYS[2])
redis.call('SET', KEYS[1], ARGV[1] .. ':' .. fence, 'PX', ARGV[2])
return fence
"""
_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""
_VALID = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return 1 end
return 0
"""


class LeaseUnavailable(RuntimeError):
    """Another worker currently owns this source."""


class LeaseLost(RuntimeError):
    """This worker no longer owns its source lease."""


class RedisSourceLease:
    def __init__(
        self, redis: Any, key: str, owner: str, fencing_token: int, ttl_ms: int
    ):
        self.redis = redis
        self.key = key
        self.owner = owner
        self.fencing_token = fencing_token
        self.ttl_ms = ttl_ms
        self._lost = asyncio.Event()
        self._heartbeat: asyncio.Task | None = None

    async def start(self) -> None:
        self._heartbeat = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(max(0.25, self.ttl_ms / 3000))
                renewed = await self.redis.eval(
                    _RENEW, 1, self.key, self.owner, self.ttl_ms
                )
                if int(renewed or 0) != 1:
                    self._lost.set()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            self._lost.set()

    async def assert_valid(self) -> None:
        if self._lost.is_set():
            raise LeaseLost("source lease heartbeat failed")
        valid = await self.redis.eval(_VALID, 1, self.key, self.owner)
        if int(valid or 0) != 1:
            self._lost.set()
            raise LeaseLost("source lease is no longer owned")

    async def close(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            try:
                await self._heartbeat
            except asyncio.CancelledError:
                pass
        try:
            await self.redis.eval(_RELEASE, 1, self.key, self.owner)
        except Exception:
            # TTL expiry is the safe fallback if Redis is unavailable during cleanup.
            logger.warning("Unable to release WIATS lease; it will expire by TTL")


class RedisSourceLeaseManager:
    """Atomic Redis lease acquisition with renewable TTL and monotonic fence."""

    def __init__(
        self, redis: Any, *, namespace: str = "aitos:wiats:lease", ttl_ms: int = 30_000
    ) -> None:
        if not namespace or ttl_ms < 3_000:
            raise ValueError("namespace and ttl_ms >= 3000 are required")
        self.redis = redis
        self.namespace = namespace.rstrip(":")
        self.ttl_ms = int(ttl_ms)

    def _keys(self, source_id: str) -> tuple[str, str]:
        if not source_id or not source_id.strip():
            raise ValueError("source_id is required")
        digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()
        return (
            f"{self.namespace}:{{{digest}}}:lease",
            f"{self.namespace}:{{{digest}}}:fence",
        )

    @asynccontextmanager
    async def hold(self, source_id: str) -> AsyncIterator[RedisSourceLease]:
        lease_key, fence_key = self._keys(source_id)
        token_owner = uuid.uuid4().hex
        token = await self.redis.eval(
            _ACQUIRE, 2, lease_key, fence_key, token_owner, self.ttl_ms
        )
        if token is None:
            raise LeaseUnavailable(f"source {source_id!r} is already leased")
        fencing_token = int(token)
        lease = RedisSourceLease(
            self.redis,
            lease_key,
            f"{token_owner}:{fencing_token}",
            fencing_token,
            self.ttl_ms,
        )
        await lease.start()
        try:
            await lease.assert_valid()
            yield lease
        finally:
            await lease.close()


__all__ = [
    "LeaseLost",
    "LeaseUnavailable",
    "RedisSourceLease",
    "RedisSourceLeaseManager",
]
