"""Read-only EVM ERC-20 metadata discovery with strict ABI decoding.

Discovered symbols are descriptive metadata only. They are never used to infer
asset identity, price symbols, token legitimacy, or beneficial ownership.
"""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from .token_registry import TokenMetadata


class EvmTokenMetadataResolver:
    """Resolve decimals/symbol/name from configured ERC-20 addresses."""

    def __init__(
        self,
        *,
        rpc_url: str,
        chain_id: str,
        timeout_seconds: float = 8.0,
        max_concurrency: int = 4,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        if not rpc_url.startswith("https://") or not chain_id.strip():
            raise ValueError("HTTPS RPC and chain id are required")
        if not 0.2 <= timeout_seconds <= 30:
            raise ValueError("timeout outside safe bounds")
        if not 1 <= max_concurrency <= 16:
            raise ValueError("metadata concurrency must be in [1, 16]")
        self.rpc_url = rpc_url
        self.chain_id = chain_id.strip()
        self.timeout_seconds = timeout_seconds
        self.max_concurrency = max_concurrency
        self.session = session
        self.owns_session = session is None
        self.request_id = 0
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def close(self) -> None:
        if self.owns_session and self.session and not self.session.closed:
            await self.session.close()

    @staticmethod
    def _address(value: str) -> str:
        normalized = value.lower()
        if (
            len(normalized) != 42
            or not normalized.startswith("0x")
            or any(char not in "0123456789abcdef" for char in normalized[2:])
        ):
            raise ValueError("invalid ERC-20 contract address")
        return normalized

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds)
            )
        async with self._semaphore:
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
            raise RuntimeError(f"EVM metadata RPC {method} failed")
        return payload.get("result")

    @staticmethod
    def _decode_uint(raw: Any) -> int:
        if not isinstance(raw, str) or not raw.startswith("0x"):
            raise ValueError("invalid ERC-20 decimals response")
        encoded = raw[2:]
        if not encoded or len(encoded) > 64 or any(
            char not in "0123456789abcdefABCDEF" for char in encoded
        ):
            raise ValueError("invalid ERC-20 decimals response")
        value = int(encoded, 16)
        if not 0 <= value <= 36:
            raise ValueError("ERC-20 decimals outside supported range [0, 36]")
        return value

    @staticmethod
    def _decode_text(raw: Any) -> str:
        if not isinstance(raw, str) or not raw.startswith("0x"):
            raise ValueError("invalid ERC-20 text response")
        encoded = raw[2:]
        if not encoded or len(encoded) > 8192 or len(encoded) % 2:
            raise ValueError("ERC-20 text response outside safe bounds")
        try:
            data = bytes.fromhex(encoded)
            # Standard ABI dynamic string: offset, byte length, UTF-8 payload.
            if len(data) >= 64:
                offset = int.from_bytes(data[:32], "big")
                if offset == 32 and len(data) >= 64:
                    length = int.from_bytes(data[32:64], "big")
                    if length <= len(data) - 64:
                        value = data[64 : 64 + length].decode("utf-8")
                    else:
                        raise ValueError("truncated ERC-20 string")
                else:
                    value = data.decode("utf-8").rstrip("\x00")
            else:
                # Some older contracts return bytes32 instead of string.
                value = data.rstrip(b"\x00").decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            raise ValueError("unable to decode ERC-20 symbol/name") from None
        value = "".join(char for char in value if char.isprintable()).strip()
        if not value or len(value) > 64:
            raise ValueError("ERC-20 symbol/name is empty or too long")
        return value

    async def resolve(self, address: str) -> TokenMetadata:
        contract = self._address(address)
        decimals_raw, symbol_raw = await asyncio.gather(
            self._rpc("eth_call", [{"to": contract, "data": "0x313ce567"}, "latest"]),
            self._rpc("eth_call", [{"to": contract, "data": "0x95d89b41"}, "latest"]),
        )
        decimals = self._decode_uint(decimals_raw)
        symbol = self._decode_text(symbol_raw)
        return TokenMetadata(
            chain_id=self.chain_id,
            address=contract,
            asset_id=f"evm:{self.chain_id}:{contract}",
            symbol=symbol,
            decimals=decimals,
            # Deliberately unset: a token symbol is not a trusted exchange pair.
            price_symbol=None,
        )

    async def resolve_many(self, addresses: list[str]) -> list[TokenMetadata]:
        if not addresses or len(addresses) > 500:
            raise ValueError("metadata discovery requires 1-500 configured addresses")
        normalized = [self._address(address) for address in addresses]
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate token address in discovery allowlist")
        return list(await asyncio.gather(*(self.resolve(address) for address in normalized)))
