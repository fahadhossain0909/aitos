"""Read-only Uniswap V2/V3 pool swap log observer for configured pools."""

from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from typing import Any
import aiohttp
from .contracts import CapitalEvent

V2_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
V3_SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
V4_SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"


class EvmDexSwapAdapter:
    """Decodes swap quantities without inventing USD value or token decimals."""

    def __init__(
        self,
        *,
        rpc_url: str,
        chain_id: str,
        pools: dict[str, str],
        v4_pool_tokens: dict[str, dict[str, str]] | None = None,
        confirmations: int = 12,
        max_block_range: int = 250,
        max_reorg_depth: int = 64,
        session: aiohttp.ClientSession | None = None,
    ):
        if not rpc_url.startswith("https://") or not pools or len(pools) > 300:
            raise ValueError("HTTPS RPC and 1-300 pools required")
        if not 1 <= confirmations <= 10000 or not 1 <= max_block_range <= 2000:
            raise ValueError("invalid block bounds")
        if not 1 <= max_reorg_depth <= 256:
            raise ValueError("invalid reorg depth")
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.pools = {self._address(k): v.lower() for k, v in pools.items()}
        if any(
            v
            not in {
                "uniswap_v2",
                "uniswap_v3",
                "pancakeswap_v2",
                "pancakeswap_v3",
                "uniswap_v4",
            }
            for v in self.pools.values()
        ):
            raise ValueError("unsupported pool protocol")
        self.v4_pool_tokens: dict[str, tuple[str, str]] = {}
        for pool_id, tokens in (v4_pool_tokens or {}).items():
            normalized_id = str(pool_id).lower()
            if (
                len(normalized_id) != 66
                or not normalized_id.startswith("0x")
                or any(char not in "0123456789abcdef" for char in normalized_id[2:])
                or not isinstance(tokens, dict)
                or not {"token0", "token1"}.issubset(tokens)
            ):
                raise ValueError("invalid Uniswap V4 pool ID/token mapping")
            token0 = self._address(tokens["token0"])
            token1 = self._address(tokens["token1"])
            if token0 >= token1:
                raise ValueError("V4 currencies must be strictly sorted")
            self.v4_pool_tokens[normalized_id] = (token0, token1)
        if self.v4_pool_tokens and not any(
            protocol == "uniswap_v4" for protocol in self.pools.values()
        ):
            raise ValueError("V4 pool tokens require an allowlisted PoolManager")
        self.confirmations = confirmations
        self.max_block_range = max_block_range
        self.max_reorg_depth = max_reorg_depth
        self.last_reorg: dict[str, Any] | None = None
        self.session = session
        self.owns_session = session is None
        self.request_id = 0
        self._pool_tokens: dict[str, tuple[str, str]] = {}

    @staticmethod
    def _address(value: str) -> str:
        value = value.lower()
        if (
            len(value) != 42
            or not value.startswith("0x")
            or any(c not in "0123456789abcdef" for c in value[2:])
        ):
            raise ValueError("invalid EVM address")
        return value

    async def close(self):
        if self.owns_session and self.session and not self.session.closed:
            await self.session.close()

    async def _rpc(self, method: str, params: list[Any]):
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
            result = await response.json()
        if result.get("error"):
            raise RuntimeError(f"EVM RPC {method} failed")
        return result.get("result")

    async def _token_addresses(self, pool: str) -> tuple[str, str]:
        cached = self._pool_tokens.get(pool)
        if cached:
            return cached
        values = []
        for selector in ("0x0dfe1681", "0xd21220a7"):
            raw = await self._rpc(
                "eth_call", [{"to": pool, "data": selector}, "latest"]
            )
            if not isinstance(raw, str) or len(raw) < 66:
                raise RuntimeError("DEX pool token metadata unavailable")
            values.append(self._address("0x" + raw[-40:]))
        if values[0] == values[1]:
            raise RuntimeError("DEX pool returned duplicate token addresses")
        self._pool_tokens[pool] = (values[0], values[1])
        return values[0], values[1]

    async def _header(self, number: int) -> dict[str, Any]:
        block = await self._rpc("eth_getBlockByNumber", [hex(number), False])
        if not block or not block.get("hash") or not block.get("timestamp"):
            raise RuntimeError("DEX canonical block header unavailable")
        return block

    async def poll(self, *, cursor: str | None = None):
        self.last_reorg = None
        latest = int(await self._rpc("eth_blockNumber", []), 16)
        safe = latest - self.confirmations
        state = None
        if cursor and cursor.startswith("{"):
            try:
                state = json.loads(cursor)
            except ValueError:
                raise ValueError("invalid DEX reorg cursor JSON") from None
            if (
                not isinstance(state, dict)
                or state.get("version") != 2
                or not isinstance(state.get("head_block"), int)
                or not isinstance(state.get("recent_hashes"), dict)
            ):
                raise ValueError("invalid DEX reorg cursor schema")
            if any(
                not str(number).isdigit()
                or not isinstance(block_hash, str)
                or not block_hash.startswith("0x")
                for number, block_hash in state["recent_hashes"].items()
            ):
                raise ValueError("invalid DEX cursor block-hash map")
        elif cursor:
            try:
                legacy_head = int(cursor, 16)
            except ValueError:
                raise ValueError(
                    "DEX cursor must be a hex block or version-2 JSON"
                ) from None
            if legacy_head < 0:
                raise ValueError("invalid DEX cursor")
            state = {"version": 1, "head_block": legacy_head, "recent_hashes": {}}

        previous_hashes = dict(state.get("recent_hashes", {})) if state else {}
        reorg_start = None
        if state is not None:
            old_head = state["head_block"]
            if old_head > safe:
                raise RuntimeError("DEX safe head regressed below persisted cursor")
            if state.get("version") == 2:
                known_head = previous_hashes.get(str(old_head))
                if not known_head:
                    raise RuntimeError("DEX cursor missing canonical head hash")
                current_head = await self._header(old_head)
                if current_head["hash"].lower() != known_head.lower():
                    ancestor = None
                    lower = max(0, old_head - self.max_reorg_depth)
                    for number in range(old_head - 1, lower - 1, -1):
                        known = previous_hashes.get(str(number))
                        if not known:
                            continue
                        header = await self._header(number)
                        if header["hash"].lower() == known.lower():
                            ancestor = number
                            break
                    if ancestor is None:
                        raise RuntimeError(
                            "DEX reorg exceeds retained canonical window; controlled backfill required"
                        )
                    reorg_start = ancestor + 1
                    if old_head - ancestor > self.max_reorg_depth:
                        raise RuntimeError("DEX reorg exceeds configured maximum depth")
                    start_block = reorg_start
                else:
                    start_block = old_head + 1
            else:
                start_block = old_head + 1
        else:
            start_block = max(0, safe - self.max_block_range + 1)

        if safe < start_block:
            return [], cursor
        end = min(safe, start_block + self.max_block_range - 1)
        if reorg_start is not None and end < state["head_block"]:
            raise RuntimeError("DEX reorg recovery range is not fully available")

        canonical_hashes: dict[int, str] = {}
        if reorg_start is not None:
            for number in range(reorg_start, end + 1):
                canonical_hashes[number] = (await self._header(number))["hash"].lower()
            self.last_reorg = {
                "source_id": f"evm:{self.chain_id}",
                "from_block": reorg_start,
                "to_block": end,
                "canonical_hashes": canonical_hashes,
                "detected_at": datetime.now(timezone.utc).isoformat(),
            }

        logs = await self._rpc(
            "eth_getLogs",
            [
                {
                    "fromBlock": hex(start_block),
                    "toBlock": hex(end),
                    "address": list(self.pools),
                    "topics": [[V2_SWAP_TOPIC, V3_SWAP_TOPIC, V4_SWAP_TOPIC]],
                }
            ],
        )
        events = []
        now = datetime.now(timezone.utc)
        block_times = {}
        for log in logs or []:
            topic = log.get("topics", [""])[0].lower()
            data = log.get("data", "0x")[2:]
            pool = self._address(log["address"])
            protocol = self.pools.get(pool)
            if protocol is None:
                continue
            if topic == V2_SWAP_TOPIC and not protocol.endswith("_v2"):
                continue
            if topic == V3_SWAP_TOPIC and not protocol.endswith("_v3"):
                continue
            if topic == V4_SWAP_TOPIC and protocol != "uniswap_v4":
                continue
            words = [data[i : i + 64] for i in range(0, len(data), 64)]

            def signed(word: str, bits: int = 256) -> int:
                value = int(word, 16) & ((1 << bits) - 1)
                return value - (1 << bits) if value >= (1 << (bits - 1)) else value

            pool_id = None
            if topic == V2_SWAP_TOPIC:
                if len(words) < 4:
                    continue
                amount0_in, amount1_in, amount0_out, amount1_out = [
                    int(word, 16) for word in words[:4]
                ]
                delta0 = amount0_in - amount0_out
                delta1 = amount1_in - amount1_out
                token0_address, token1_address = await self._token_addresses(pool)
            elif topic == V3_SWAP_TOPIC:
                if len(words) < 5:
                    continue
                delta0, delta1 = signed(words[0]), signed(words[1])
                token0_address, token1_address = await self._token_addresses(pool)
            elif topic == V4_SWAP_TOPIC:
                topics = log.get("topics", [])
                if len(topics) < 3 or len(words) < 6:
                    continue
                pool_id = str(topics[1]).lower()
                token_pair = self.v4_pool_tokens.get(pool_id)
                if token_pair is None:
                    continue
                token0_address, token1_address = token_pair
                delta0, delta1 = signed(words[0], 128), signed(words[1], 128)
            else:
                continue
            block = int(log["blockNumber"], 16)
            tx = log["transactionHash"]
            index = int(log["logIndex"], 16)
            if block not in block_times:
                block_data = await self._header(block)
                block_times[block] = datetime.fromtimestamp(
                    int(block_data["timestamp"], 16), timezone.utc
                )
                canonical_hash = block_data["hash"].lower()
                if canonical_hashes and block in canonical_hashes:
                    if canonical_hashes[block] != canonical_hash:
                        raise RuntimeError("DEX canonical hash changed during scan")
                canonical_hashes[block] = canonical_hash
            block_hash = str(log.get("blockHash") or "").lower()
            if block_hash and block_hash != canonical_hashes[block]:
                raise RuntimeError("DEX log block hash changed during poll; retry")
            event_id = hashlib.sha256(
                f"{self.chain_id}|{tx}|{index}".encode()
            ).hexdigest()
            actor_topic_index = 2 if topic == V4_SWAP_TOPIC else 1
            actor = (
                "0x" + log["topics"][actor_topic_index][-40:]
                if len(log.get("topics", [])) > actor_topic_index
                else None
            )
            events.append(
                CapitalEvent(
                    event_id,
                    f"evm:{self.chain_id}",
                    "dex_swap",
                    "crypto_spot",
                    self.pools[pool],
                    pool,
                    "pool_swap",
                    block_times[block],
                    now,
                    abs(delta0),
                    "token0_base_units",
                    None,
                    actor_id=actor,
                    transaction_id=tx,
                    block_number=block,
                    block_hash=canonical_hashes[block],
                    finality="confirmed",
                    metadata={
                        "chain_id": self.chain_id,
                        "asset_address": token0_address,
                        "token0_address": token0_address,
                        "token1_address": token1_address,
                        "raw_delta": str(delta0),
                        "amount0_delta": str(delta0),
                        "amount1_delta": str(delta1),
                        "quantity_semantics": "absolute token0 delta only; token1 kept separately",
                        "actor_is_pool_caller_not_verified_beneficial_owner": True,
                        "log_index": index,
                        "token0_and_token1_decimals_required": True,
                        "pool": pool,
                        "pool_id": pool_id,
                        "pool_manager": pool if topic == V4_SWAP_TOPIC else None,
                        "sqrt_price_x96": int(words[2], 16)
                        if topic == V4_SWAP_TOPIC
                        else None,
                        "post_swap_liquidity": int(words[3], 16)
                        if topic == V4_SWAP_TOPIC
                        else None,
                        "post_swap_tick": signed(words[4], 24)
                        if topic == V4_SWAP_TOPIC
                        else None,
                        "swap_fee_pips": int(words[5], 16)
                        if topic == V4_SWAP_TOPIC
                        else None,
                    },
                )
            )

        window_start = max(0, end - self.max_reorg_depth)
        retained = {
            str(number): block_hash
            for number, block_hash in previous_hashes.items()
            if int(number) >= window_start and int(number) < start_block
        }
        for number in range(window_start, end + 1):
            if str(number) in retained:
                continue
            if number in canonical_hashes:
                retained[str(number)] = canonical_hashes[number]
            else:
                retained[str(number)] = (await self._header(number))["hash"].lower()
        next_cursor = json.dumps(
            {"version": 2, "head_block": end, "recent_hashes": retained},
            sort_keys=True,
            separators=(",", ":"),
        )
        return events, next_cursor
