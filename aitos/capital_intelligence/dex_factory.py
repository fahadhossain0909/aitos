"""Bounded EVM DEX factory discovery with canonical reorg recovery.

Pool creation logs are observations only. Discovered pools are not automatically
activated for swap ingestion; operators must review protocol/factory allowlists.
"""

from __future__ import annotations

import hashlib
import json
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
V4_POOL_INITIALIZED_TOPIC = (
    "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
)


class EvmDexFactoryDiscoveryAdapter:
    """Read-only PairCreated/PoolCreated discovery for configured factories.

    The cursor retains a contiguous canonical block-hash window. A head change
    searches for a common ancestor and replays the orphaned range. Deep or
    incomplete reorganizations fail closed before the worker can checkpoint.
    """

    SUPPORTED = {
        "uniswap_v2",
        "uniswap_v3",
        "pancakeswap_v2",
        "pancakeswap_v3",
        "uniswap_v4",
    }

    def __init__(
        self,
        *,
        rpc_url: str,
        chain_id: str,
        factories: dict[str, str],
        confirmations: int = 12,
        max_block_range: int = 500,
        max_reorg_depth: int = 64,
        start_block: int | None = None,
        timeout_seconds: float = 10.0,
        session: aiohttp.ClientSession | None = None,
    ):
        if not rpc_url.startswith("https://") or not factories or len(factories) > 100:
            raise ValueError("HTTPS RPC and 1-100 factories required")
        if not chain_id.strip() or not 1 <= confirmations <= 10_000:
            raise ValueError("invalid chain or confirmation depth")
        if not 1 <= max_block_range <= 2_000:
            raise ValueError("invalid factory scan range")
        if not 1 <= max_reorg_depth <= 256:
            raise ValueError("invalid factory reorg depth")
        if start_block is not None and (
            isinstance(start_block, bool)
            or not isinstance(start_block, int)
            or start_block < 0
        ):
            raise ValueError("start_block must be a non-negative integer")
        if not 0.2 <= timeout_seconds <= 30:
            raise ValueError("invalid timeout")
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.factories = {
            self._address(key): str(value).lower() for key, value in factories.items()
        }
        if any(value not in self.SUPPORTED for value in self.factories.values()):
            raise ValueError("unsupported DEX factory protocol")
        self.confirmations = confirmations
        self.max_block_range = max_block_range
        self.max_reorg_depth = max_reorg_depth
        self.start_block = start_block
        self.timeout_seconds = timeout_seconds
        self.session = session
        self.owns_session = session is None
        self.request_id = 0
        self.last_reorg: dict[str, Any] | None = None

    @staticmethod
    def _address(value: str) -> str:
        value = str(value).lower()
        if (
            len(value) != 42
            or not value.startswith("0x")
            or any(char not in "0123456789abcdef" for char in value[2:])
        ):
            raise ValueError("invalid EVM address")
        return value

    @staticmethod
    def _decode_cursor(cursor: str | None) -> dict[str, Any] | None:
        if cursor is None:
            return None
        if cursor.startswith("{"):
            try:
                value = json.loads(cursor)
            except ValueError:
                raise ValueError("invalid DEX factory cursor JSON") from None
            if (
                not isinstance(value, dict)
                or value.get("version") != 2
                or not isinstance(value.get("head_block"), int)
                or not isinstance(value.get("recent_hashes"), dict)
            ):
                raise ValueError("invalid DEX factory cursor schema")
            hashes = value["recent_hashes"]
            if any(
                not str(number).isdigit()
                or not isinstance(block_hash, str)
                or not block_hash.startswith("0x")
                for number, block_hash in hashes.items()
            ):
                raise ValueError("invalid DEX factory cursor hash map")
            return value
        try:
            head = int(cursor, 16)
        except (TypeError, ValueError):
            raise ValueError(
                "factory cursor must be a hex block or version-2 JSON"
            ) from None
        if head < 0:
            raise ValueError("invalid factory cursor")
        return {"version": 1, "head_block": head, "recent_hashes": {}}

    async def close(self) -> None:
        if self.owns_session and self.session and not self.session.closed:
            await self.session.close()

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds)
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

    async def _header(self, number: int) -> dict[str, Any]:
        header = await self._rpc("eth_getBlockByNumber", [hex(number), False])
        if not header or not header.get("hash") or not header.get("timestamp"):
            raise RuntimeError("factory event canonical block unavailable")
        return header

    async def poll(self, *, cursor: str | None = None):
        self.last_reorg = None
        latest = int(await self._rpc("eth_blockNumber", []), 16)
        safe = latest - self.confirmations
        state = self._decode_cursor(cursor)
        previous_hashes = dict(state.get("recent_hashes", {})) if state else {}
        reorg_start = None

        if state is not None:
            old_head = state["head_block"]
            if old_head > safe:
                raise RuntimeError(
                    "DEX factory safe head regressed below persisted cursor"
                )
            if state.get("version") == 2:
                old_head_hash = previous_hashes.get(str(old_head))
                if not old_head_hash:
                    raise RuntimeError("DEX factory cursor is missing its head hash")
                current_head = await self._header(old_head)
                if current_head["hash"].lower() != old_head_hash.lower():
                    ancestor = None
                    lower_bound = max(0, old_head - self.max_reorg_depth)
                    for number in range(old_head - 1, lower_bound - 1, -1):
                        known_hash = previous_hashes.get(str(number))
                        if not known_hash:
                            continue
                        header = await self._header(number)
                        if header["hash"].lower() == known_hash.lower():
                            ancestor = number
                            break
                    if ancestor is None:
                        raise RuntimeError(
                            "DEX factory reorg exceeds retained canonical window; "
                            "controlled backfill required"
                        )
                    reorg_start = ancestor + 1
                    start = reorg_start
                else:
                    start = old_head + 1
            else:
                # Legacy hex cursors are upgraded to hash-aware version 2 after
                # one successful poll; they cannot prove whether older blocks reorged.
                start = old_head + 1
        else:
            start = (
                self.start_block
                if self.start_block is not None
                else max(0, safe - self.max_block_range + 1)
            )

        if safe < start:
            return [], cursor
        end = min(safe, start + self.max_block_range - 1)
        if reorg_start is not None and end < state["head_block"]:
            raise RuntimeError(
                "DEX factory reorg recovery exceeds one bounded scan; "
                "increase max_block_range or perform controlled backfill"
            )

        logs = await self._rpc(
            "eth_getLogs",
            [
                {
                    "fromBlock": hex(start),
                    "toBlock": hex(end),
                    "address": list(self.factories),
                    "topics": [[
                        V2_PAIR_CREATED_TOPIC,
                        V3_POOL_CREATED_TOPIC,
                        V4_POOL_INITIALIZED_TOPIC,
                    ]],
                }
            ],
        )
        now = datetime.now(timezone.utc)
        block_headers: dict[int, dict[str, Any]] = {}
        events = []
        for log in logs or []:
            topics = log.get("topics") or []
            if len(topics) < 3:
                continue
            factory = self._address(log.get("address", ""))
            protocol = self.factories.get(factory)
            topic = str(topics[0]).lower()
            if protocol is None:
                continue
            is_v2 = protocol.endswith("_v2")
            is_v3 = protocol.endswith("_v3")
            is_v4 = protocol == "uniswap_v4"
            if (
                (topic == V2_PAIR_CREATED_TOPIC and not is_v2)
                or (topic == V3_POOL_CREATED_TOPIC and not is_v3)
                or (topic == V4_POOL_INITIALIZED_TOPIC and not is_v4)
                or topic
                not in {
                    V2_PAIR_CREATED_TOPIC,
                    V3_POOL_CREATED_TOPIC,
                    V4_POOL_INITIALIZED_TOPIC,
                }
            ):
                continue

            data = str(log.get("data", "0x"))[2:]
            words = [data[index : index + 64] for index in range(0, len(data), 64)]
            if is_v4:
                if len(topics) < 4 or len(words) < 5:
                    continue
                pool_id = str(topics[1]).lower()
                token0 = self._address("0x" + str(topics[2])[-40:])
                token1 = self._address("0x" + str(topics[3])[-40:])
                if token0 >= token1:
                    continue
                fee = int(words[0], 16)
                tick_spacing_raw = int(words[1], 16)
                tick_spacing = (
                    tick_spacing_raw - (1 << 256)
                    if tick_spacing_raw >= (1 << 255)
                    else tick_spacing_raw
                )
                hooks = self._address("0x" + words[2][-40:])
                pool = pool_id
            else:
                if len(topics) < 3:
                    continue
                token0 = self._address("0x" + str(topics[1])[-40:])
                token1 = self._address("0x" + str(topics[2])[-40:])
                if token0 == token1:
                    continue
                if len(words) < 2:
                    continue
                pool_word = words[0] if is_v2 else words[1]
                pool = self._address("0x" + pool_word[-40:])
                fee = None
                tick_spacing = None
                hooks = None
            block = int(log["blockNumber"], 16)
            if block not in block_headers:
                block_headers[block] = await self._header(block)
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
                "factory_event": (
                    "Initialize" if is_v4 else "PairCreated" if is_v2 else "PoolCreated"
                ),
                "log_index": log_index,
                "operator_review_required": True,
                "pool_not_auto_activated": True,
            }
            if is_v3 and len(topics) > 3:
                metadata["fee_tier"] = int(topics[3], 16)
                tick_spacing_raw = int(words[0], 16)
                metadata["tick_spacing"] = (
                    tick_spacing_raw - (1 << 256)
                    if tick_spacing_raw >= (1 << 255)
                    else tick_spacing_raw
                )
            if is_v4:
                metadata.update(
                    {
                        "pool_id": pool_id,
                        "pool_manager": factory,
                        "fee_tier": fee,
                        "tick_spacing": tick_spacing,
                        "hooks_address": hooks,
                        "singleton_pool": True,
                        "native_currency_supported": token0
                        == "0x" + "0" * 40
                        or token1 == "0x" + "0" * 40,
                    }
                )
            events.append(
                CapitalEvent(
                    event_id,
                    f"evm:{self.chain_id}",
                    "dex_pool_discovery",
                    "crypto_spot",
                    protocol,
                    pool,
                    "dex_pool_initialized" if is_v4 else "dex_pool_created",
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

        reorg_canonical_hashes = {}
        if reorg_start is not None:
            for number in range(reorg_start, end + 1):
                header = block_headers.get(number) or await self._header(number)
                block_headers[number] = header
                reorg_canonical_hashes[number] = str(header["hash"]).lower()

        # Retain a contiguous window of max_reorg_depth + 1 canonical headers.
        # Reuse the prior canonical prefix and fetch only newly scanned blocks.
        window_start = max(0, end - self.max_reorg_depth)
        recent_hashes = {
            str(number): block_hash
            for number, block_hash in previous_hashes.items()
            if window_start <= int(number) < start
        }
        for number in range(window_start, end + 1):
            if str(number) in recent_hashes:
                continue
            header = block_headers.get(number) or await self._header(number)
            recent_hashes[str(number)] = str(header["hash"]).lower()

        if reorg_start is not None:
            self.last_reorg = {
                "source_id": f"evm:{self.chain_id}",
                "from_block": reorg_start,
                "to_block": end,
                "canonical_hashes": reorg_canonical_hashes,
                "detected_at": now.isoformat(),
            }

        next_cursor = json.dumps(
            {
                "version": 2,
                "head_block": end,
                "recent_hashes": recent_hashes,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return events, next_cursor
