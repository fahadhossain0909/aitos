"""Bounded EVM ERC-20 observer with canonical block-hash reconciliation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .contracts import CapitalEvent

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class EVMLogCapitalAdapter:
    """Collect confirmation-delayed transfers and detect bounded chain reorganizations.

    Version-2 cursors retain a contiguous recent block-hash window. If the previous
    head hash changes, the adapter searches backwards for a common ancestor, rescans
    the affected range and exposes a reorg reconciliation instruction to the worker.
    A reorg deeper than the configured window fails closed and requires backfill.
    """

    def __init__(
        self,
        *,
        rpc_url: str,
        chain_id: str,
        contracts: list[str],
        confirmations: int = 12,
        max_block_range: int = 500,
        max_reorg_depth: int = 64,
        timeout_seconds: float = 10.0,
        session: aiohttp.ClientSession | None = None,
    ):
        if not rpc_url.startswith("https://"):
            raise ValueError("RPC URL must use HTTPS")
        if not chain_id.strip() or not contracts or len(contracts) > 500:
            raise ValueError("chain and 1-500 contracts required")
        if not 1 <= confirmations <= 10000 or not 1 <= max_block_range <= 5000:
            raise ValueError("invalid block bounds")
        if not 1 <= max_reorg_depth <= 256:
            raise ValueError("reorg depth must be in [1, 256]")
        if not 0.2 <= timeout_seconds <= 30:
            raise ValueError("invalid timeout")
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.contracts = sorted({self._address(address) for address in contracts})
        self.confirmations = confirmations
        self.max_block_range = max_block_range
        self.max_reorg_depth = max_reorg_depth
        self.timeout_seconds = timeout_seconds
        self.session = session
        self.owns_session = session is None
        self.request_id = 0
        self.last_reorg: dict[str, Any] | None = None

    @staticmethod
    def _address(value: str) -> str:
        value = value.lower()
        if (
            len(value) != 42
            or not value.startswith("0x")
            or any(char not in "0123456789abcdef" for char in value[2:])
        ):
            raise ValueError("invalid EVM address")
        return value

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
        block = await self._rpc("eth_getBlockByNumber", [hex(number), False])
        if not block or not block.get("hash") or not block.get("timestamp"):
            raise RuntimeError("EVM canonical block header unavailable")
        return block

    @staticmethod
    def _decode_cursor(cursor: str | None) -> dict[str, Any] | None:
        if cursor is None:
            return None
        if cursor.startswith("{"):
            try:
                value = json.loads(cursor)
            except ValueError:
                raise ValueError("invalid EVM reorg cursor JSON") from None
            if (
                not isinstance(value, dict)
                or value.get("version") != 2
                or not isinstance(value.get("head_block"), int)
                or not isinstance(value.get("recent_hashes"), dict)
            ):
                raise ValueError("invalid EVM reorg cursor schema")
            hashes = value["recent_hashes"]
            if any(
                not str(key).isdigit()
                or not isinstance(block_hash, str)
                or not block_hash.startswith("0x")
                for key, block_hash in hashes.items()
            ):
                raise ValueError("invalid EVM cursor block-hash map")
            return value
        try:
            legacy_head = int(cursor, 16)
        except (TypeError, ValueError):
            raise ValueError(
                "EVM cursor must be a hex block or version-2 JSON"
            ) from None
        if legacy_head < 0:
            raise ValueError("invalid EVM cursor")
        return {"version": 1, "head_block": legacy_head, "recent_hashes": {}}

    async def poll(
        self, *, cursor: str | None = None
    ) -> tuple[list[CapitalEvent], str | None]:
        self.last_reorg = None
        latest_raw = await self._rpc("eth_blockNumber", [])
        latest = int(latest_raw, 16)
        safe_head = latest - self.confirmations
        state = self._decode_cursor(cursor)
        previous_hashes = dict(state.get("recent_hashes", {})) if state else {}
        reorg_start = None

        if state is not None:
            old_head = state["head_block"]
            if old_head > safe_head:
                raise RuntimeError(
                    "EVM safe head regressed below persisted cursor; refusing cursor rollback"
                )
            if state.get("version") == 2:
                old_head_hash = previous_hashes.get(str(old_head))
                if not old_head_hash:
                    raise RuntimeError("EVM cursor is missing its canonical head hash")
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
                            "EVM reorg exceeds retained canonical window; controlled backfill required"
                        )
                    reorg_start = ancestor + 1
                    if old_head - ancestor > self.max_reorg_depth:
                        raise RuntimeError(
                            "EVM reorg exceeds configured maximum depth; controlled backfill required"
                        )
                    start = reorg_start
                else:
                    start = old_head + 1
            else:
                start = old_head + 1
        else:
            start = max(0, safe_head - self.max_block_range + 1)

        if safe_head < start:
            return [], cursor
        end = min(safe_head, start + self.max_block_range - 1)
        if reorg_start is not None and end < state["head_block"]:
            raise RuntimeError("EVM reorg recovery range is not fully available")

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
                    "fromBlock": hex(start),
                    "toBlock": hex(end),
                    "address": self.contracts,
                    "topics": [TRANSFER_TOPIC],
                }
            ],
        )
        now = datetime.now(timezone.utc)
        block_headers: dict[int, dict[str, Any]] = {}
        events: list[CapitalEvent] = []
        for log in logs or []:
            topics = log.get("topics", [])
            if len(topics) != 3:
                continue
            block_number = int(log["blockNumber"], 16)
            if block_number not in block_headers:
                block_headers[block_number] = await self._header(block_number)
            canonical_hash = block_headers[block_number]["hash"].lower()
            log_hash = str(log.get("blockHash") or "").lower()
            if log_hash and log_hash != canonical_hash:
                raise RuntimeError(
                    "EVM log block hash changed during poll; retry required"
                )
            if canonical_hashes and block_number in canonical_hashes:
                if canonical_hashes[block_number] != canonical_hash:
                    raise RuntimeError("EVM canonical hash changed during reorg scan")
            canonical_hashes[block_number] = canonical_hash
            block_time = datetime.fromtimestamp(
                int(block_headers[block_number]["timestamp"], 16), timezone.utc
            )
            tx_hash = log["transactionHash"]
            log_index = int(log["logIndex"], 16)
            amount = int(log.get("data", "0x0"), 16)
            source = "0x" + topics[1][-40:]
            destination = "0x" + topics[2][-40:]
            token = self._address(log["address"])
            event_id = hashlib.sha256(
                f"{self.chain_id}|{tx_hash}|{log_index}".encode()
            ).hexdigest()
            events.append(
                CapitalEvent(
                    event_id,
                    f"evm:{self.chain_id}",
                    "onchain_transfer",
                    "crypto_spot",
                    self.chain_id,
                    token,
                    "erc20_transfer",
                    block_time,
                    now,
                    float(amount),
                    "token_base_units",
                    None,
                    source,
                    destination,
                    tx_hash,
                    block_number,
                    canonical_hash,
                    "confirmed",
                    1.0,
                    {
                        "log_index": log_index,
                        "actor_identity_kind": "evm_address",
                        "raw_amount": str(amount),
                        "decimals_required": True,
                        "direction_semantics": "transfer_only_not_trade_intent",
                    },
                )
            )

        # Retain a contiguous hash window so the next poll can find a common ancestor.
        # Keep one extra ancestor hash so a reorg exactly at the configured depth
        # can still be anchored to the last common canonical block.
        window_start = max(0, end - self.max_reorg_depth)
        retained = {
            str(number): block_hash
            for number, block_hash in previous_hashes.items()
            if int(number) >= window_start and int(number) < start
        }
        for number in range(window_start, end + 1):
            if str(number) in retained:
                continue
            if number in canonical_hashes:
                retained[str(number)] = canonical_hashes[number]
            elif number in block_headers:
                retained[str(number)] = block_headers[number]["hash"].lower()
            else:
                retained[str(number)] = (await self._header(number))["hash"].lower()
        next_cursor = json.dumps(
            {
                "version": 2,
                "head_block": end,
                "recent_hashes": retained,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return events, next_cursor
