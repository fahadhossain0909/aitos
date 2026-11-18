"""Operator-managed, chain-scoped CFEI entity watchlist backed by Redis."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class RedisEntityWatchlist:
    """Small bounded watchlist; entries are analyst bookmarks, never identity claims."""

    def __init__(
        self,
        redis_client: Any,
        *,
        key: str = "aitos:cfei:watchlist",
        max_entries: int = 5000,
    ):
        if not key or any(char.isspace() for char in key):
            raise ValueError("invalid watchlist key")
        if isinstance(max_entries, bool) or not 1 <= max_entries <= 50_000:
            raise ValueError("max_entries must be between 1 and 50000")
        self.redis = redis_client
        self.key = key
        self.max_entries = max_entries

    @staticmethod
    def _identity(chain: str, address: str) -> tuple[str, str, str]:
        if not isinstance(chain, str) or not isinstance(address, str):
            raise ValueError("chain and address must be strings")
        chain = chain.strip().lower()
        address = address.strip()
        if (
            not chain
            or len(chain) > 64
            or any(char.isspace() for char in chain)
            or not address
            or len(address) > 128
            or any(char.isspace() for char in address)
        ):
            raise ValueError("invalid chain or address")
        # EVM addresses are case-insensitive; preserve case for base58 chains.
        if _EVM_ADDRESS.fullmatch(address):
            address = address.lower()
        return chain, address, f"{chain}:{address}"

    async def add(self, chain: str, address: str, note: str = "") -> dict[str, Any]:
        chain, address, entity_id = self._identity(chain, address)
        if not isinstance(note, str):
            raise ValueError("watchlist note must be a string")
        note = note.strip()
        if len(note) > 280:
            raise ValueError("watchlist note must be at most 280 characters")
        record = {
            "entity_id": entity_id,
            "chain": chain,
            "address": address,
            "note": note,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        payload = json.dumps(record, separators=(",", ":"), allow_nan=False)
        script = """
        if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 0
           and redis.call('HLEN', KEYS[1]) >= tonumber(ARGV[3]) then
            return 0
        end
        redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
        return 1
        """
        added = await self.redis.eval(
            script, 1, self.key, entity_id, payload, self.max_entries
        )
        if not added:
            raise ValueError("watchlist capacity reached")
        return record

    async def get(self, chain: str, address: str) -> dict[str, Any] | None:
        _, _, entity_id = self._identity(chain, address)
        raw = await self.redis.hget(self.key, entity_id)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    async def remove(self, chain: str, address: str) -> bool:
        _, _, entity_id = self._identity(chain, address)
        return bool(await self.redis.hdel(self.key, entity_id))

    async def list(self, *, limit: int = 500) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not 1 <= limit <= 5000:
            raise ValueError("watchlist limit must be between 1 and 5000")
        rows = await self.redis.hgetall(self.key)
        decoded = []
        for raw in rows.values():
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            item = json.loads(raw)
            decoded.append(item)
        decoded.sort(key=lambda item: (item["chain"], item["address"]))
        return decoded[:limit]
