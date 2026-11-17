"""Bounded EVM ERC-20 Transfer observer over public JSON-RPC."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .contracts import CapitalEvent

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class EVMLogCapitalAdapter:
    """Collects finalized-by-confirmation-depth transfer logs for configured tokens.

    Cursor is the last completely scanned block (hex). The caller must persist it
    only after the event batch has been durably appended.
    """

    def __init__(
        self,
        *,
        rpc_url: str,
        chain_id: str,
        contracts: list[str],
        confirmations: int = 12,
        max_block_range: int = 500,
        timeout_seconds: float = 10.0,
        session: aiohttp.ClientSession | None = None,
    ):
        if not rpc_url.startswith("https://"):
            raise ValueError("RPC URL must use HTTPS")
        if not chain_id.strip() or not contracts or len(contracts) > 500:
            raise ValueError("chain and 1-500 contracts required")
        if not 1 <= confirmations <= 10000 or not 1 <= max_block_range <= 5000:
            raise ValueError("invalid block bounds")
        if not 0.2 <= timeout_seconds <= 30:
            raise ValueError("invalid timeout")
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.contracts = sorted({self._address(address) for address in contracts})
        self.confirmations = confirmations
        self.max_block_range = max_block_range
        self.timeout_seconds = timeout_seconds
        self.session = session
        self.owns_session = session is None
        self.request_id = 0

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

    async def poll(
        self, *, cursor: str | None = None
    ) -> tuple[list[CapitalEvent], str | None]:
        latest_raw = await self._rpc("eth_blockNumber", [])
        latest = int(latest_raw, 16)
        safe_head = latest - self.confirmations
        if cursor is not None:
            try:
                start = int(cursor, 16) + 1
            except (TypeError, ValueError):
                raise ValueError("EVM cursor must be a hex block number") from None
        else:
            start = max(0, safe_head - self.max_block_range + 1)
        if safe_head < start:
            return [], cursor
        end = min(safe_head, start + self.max_block_range - 1)
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
        block_times: dict[int, datetime] = {}
        events: list[CapitalEvent] = []
        for log in logs or []:
            topics = log.get("topics", [])
            if len(topics) != 3:
                continue
            block_number = int(log["blockNumber"], 16)
            if block_number not in block_times:
                block = await self._rpc(
                    "eth_getBlockByNumber", [hex(block_number), False]
                )
                if not block or not block.get("timestamp"):
                    raise RuntimeError(
                        "EVM block timestamp unavailable; refusing bad event"
                    )
                block_times[block_number] = datetime.fromtimestamp(
                    int(block["timestamp"], 16), timezone.utc
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
                    block_times[block_number],
                    now,
                    float(amount),
                    "token_base_units",
                    None,
                    source,
                    destination,
                    tx_hash,
                    block_number,
                    log.get("blockHash"),
                    "confirmed",
                    1.0,
                    {
                        "log_index": log_index,
                        "raw_amount": str(amount),
                        "decimals_required": True,
                        "direction_semantics": "transfer_only_not_trade_intent",
                    },
                )
            )
        return events, hex(end)
