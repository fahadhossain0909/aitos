"""Bounded EVM DEX factory discovery for V2/V3-compatible deployments.

Pool creation logs are observations only. Discovered pools are not automatically
activated for swap ingestion; operators must review protocol/factory allowlists.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .contracts import CapitalEvent

V2_PAIR_CREATED_TOPIC = (
    "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
)
V3_POOL_CREATED_TOPIC = (
    "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
)


class EvmDexFactoryDiscoveryAdapter:
    """Read-only PairCreated/PoolCreated discovery for configured factories."""

    SUPPORTED = {
        "uniswap_v2",
        "uniswap_v3",
        "pancakeswap_v2",
        "pancakeswap_v3",
    }

    def __init__(
        self,
        *,
        rpc_url: str,
        chain_id: str,
        factories: dict[str, str],
        confirmations: int = 12,
        max_block_range: int = 500,
        session: aiohttp.ClientSession | None = None,
    ):
        if not rpc_url.startswith("https://") or not factories or len(factories) > 100:
            raise ValueError("HTTPS RPC and 1-100 factories required")
        if not chain_id.strip() or not 1 <= confirmations <= 10_000:
            raise ValueError("invalid chain or confirmation depth")
        if not 1 <= max_block_range <= 2_000:
            raise ValueError("invalid factory scan range")
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.factories = {
            self._address(key): str(value).lower()
            for key, value in factories.items()
        }
        if any(value not in self.SUPPORTED for value in self.factories.values()):
            raise ValueError("unsupported DEX factory protocol")
        self.confirmations = confirmations
        self.max_block_range = max_block_range
        self.session = session
        self.owns_session = session is None
        self.request_id = 0

    @staticmethod
    def _address(value: str) -> str:
        value = str(value).lower()
        if len(value) != 42 or not value.startswith("0x") or any(
            char not in "0123456789abcdef" for char in value[2:]
        ):
            raise ValueError("invalid EVM address")
        return value

    async def close(self) -> None:
        if self.owns_session and self.session and not self.session.closed:
            await self.session.close()

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        self.request_id += 1
        async with self.session.post(
            self.rpc_url,
            json={
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": method,
                "params": params,
            },
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        if payload.get("error"):
            raise RuntimeError(f"EVM RPC {method} failed")
        return payload.get("result")

    async def poll(self, *, cursor: str | None = None):
        latest = int(await self._rpc("eth_blockNumber", []), 16)
        safe = latest - self.confirmations
        if cursor:
            try:
                start = int(cursor, 16) + 1
            except ValueError:
                raise ValueError("factory cursor must be a hex block") from None
        else:
            start = max(0, safe - self.max_block_range + 1)
        if safe < start:
            return [], cursor
        end = min(safe, start + self.max_block_range - 1)
        logs = await self._rpc(
            "eth_getLogs",
            [
                {
                    "fromBlock": hex(start),
                    "toBlock": hex(end),
                    "address": list(self.factories),
                    "topics": [[V2_PAIR_CREATED_TOPIC, V3_POOL_CREATED_TOPIC]],
                }
            ],
        )
        events = []
        now = datetime.now(timezone.utc)
        block_headers: dict[int, dict[str, Any]] = {}
        for log in logs or []:
            topics = log.get("topics") or []
            if len(topics) < 3:
                continue
            factory = self._address(log.get("address", ""))
            protocol = self.factories.get(factory)
            topic = str(topics[0]).lower()
            is_v2 = protocol is not None and protocol.endswith("_v2")
            is_v3 = protocol is not None and protocol.endswith("_v3")
            if protocol is None or (topic == V2_PAIR_CREATED_TOPIC and not is_v2):
                continue
            if topic == V3_POOL_CREATED_TOPIC and not is_v3:
                continue
            if topic not in {V2_PAIR_CREATED_TOPIC, V3_POOL_CREATED_TOPIC}:
                continue
            token0 = self._address("0x" + str(topics[1])[-40:])
            token1 = self._address("0x" + str(topics[2])[-40:])
            if token0 == token1:
                continue
            data = str(log.get("data", "0x"))[2:]
            words = [data[index:index + 64] for index in range(0, len(data), 64)]
            if len(words) < 2:
                continue
            pool_word = words[0] if is_v2 else words[1]
            pool = self._address("0x" + pool_word[-40:])
            block = int(log["blockNumber"], 16)
            if block not in block_headers:
                header = await self._rpc(
                    "eth_getBlockByNumber", [hex(block), False]
                )
                if not header or not header.get("hash") or not header.get("timestamp"):
                    raise RuntimeError("factory event canonical block unavailable")
                block_headers[block] = header
            canonical_hash = str(block_headers[block]["hash"]).lower()
            supplied_hash = str(log.get("blockHash") or "").lower()
            if supplied_hash and supplied_hash != canonical_hash:
                raise RuntimeError("factory log block hash changed during poll")
            tx_hash = str(log["transactionHash"])
            log_index = int(log["logIndex"], 16)
            event_id = hashlib.sha256(
                f"{self.chain_id}|{tx_hash}|{log_index}".encode()
            ).hexdigest()
            timestamp = datetime.fromtimestamp(
                int(block_headers[block]["timestamp"], 16), timezone.utc
            )
            metadata = {
                "factory": factory,
                "pool": pool,
                "protocol": protocol,
                "token0_address": token0,
                "token1_address": token1,
                "factory_event": "PairCreated" if is_v2 else "PoolCreated",
                "log_index": log_index,
                "operator_review_required": True,
                "pool_not_auto_activated": True,
            }
            if is_v3 and len(topics) > 3:
                metadata["fee_tier"] = int(topics[3], 16)
                metadata["tick_spacing"] = int(words[0], 16) - (
                    1 << 256 if int(words[0], 16) >= (1 << 255) else 0
                )
            events.append(
                CapitalEvent(
                    event_id,
                    f"evm:{self.chain_id}",
                    "dex_pool_discovery",
                    "crypto_spot",
                    protocol,
                    pool,
                    "dex_pool_created",
                    timestamp,
                    now,
                    0,
                    "pool_registry",
                    None,
                    transaction_id=tx_hash,
                    block_number=block,
                    block_hash=canonical_hash,
                    finality="confirmed",
                    metadata=metadata,
                )
            )
        return events, hex(end)
