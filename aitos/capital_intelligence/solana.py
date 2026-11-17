"""Read-only Solana finalized address activity and SPL-token balance deltas."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .contracts import CapitalEvent


class SolanaAddressActivityAdapter:
    """Per-address cursor observer; a cursor map avoids cross-wallet data loss."""

    def __init__(
        self,
        *,
        rpc_url: str,
        addresses: list[str],
        timeout_seconds: float = 10.0,
        page_limit: int = 100,
        session: aiohttp.ClientSession | None = None,
    ):
        if not rpc_url.startswith("https://") or not addresses or len(addresses) > 500:
            raise ValueError("HTTPS RPC and 1-500 addresses required")
        if not 1 <= page_limit <= 1000 or not 0.2 <= timeout_seconds <= 30:
            raise ValueError("invalid polling bounds")
        self.rpc_url = rpc_url
        self.addresses = sorted(set(addresses))
        self.timeout_seconds = timeout_seconds
        self.page_limit = page_limit
        self.session = session
        self.owns_session = session is None
        self.request_id = 0

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
            raise RuntimeError(f"Solana RPC {method} failed")
        return payload.get("result")

    @staticmethod
    def _cursor(cursor: str | None) -> dict[str, str]:
        if cursor is None:
            return {}
        try:
            value = json.loads(cursor)
        except (TypeError, ValueError):
            raise ValueError("Solana cursor must be a JSON address-to-signature map") from None
        if not isinstance(value, dict) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in value.items()
        ):
            raise ValueError("invalid Solana cursor map")
        return value

    @staticmethod
    def _account_key(value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("pubkey", ""))
        return str(value)

    async def _address_events(
        self, address: str, previous: str | None, observed_at: datetime
    ) -> tuple[list[CapitalEvent], str | None]:
        before = None
        signatures: list[str] = []
        newest = None
        found_cursor = previous is None
        while True:
            options: dict[str, Any] = {
                "limit": self.page_limit,
                "commitment": "finalized",
            }
            if before:
                options["before"] = before
            rows = await self._rpc("getSignaturesForAddress", [address, options])
            if not rows:
                break
            stop = False
            for row in rows:
                signature = row.get("signature")
                if not signature:
                    continue
                if signature == previous:
                    found_cursor = True
                    stop = True
                    break
                signatures.append(signature)
                if newest is None:
                    newest = signature
            if stop or len(rows) < self.page_limit:
                break
            before = rows[-1].get("signature")
            if not before:
                break
        if previous is not None and not found_cursor and signatures:
            # A gap larger than the configured page window is not silently skipped.
            raise RuntimeError(
                f"Solana cursor for {address} fell outside scan window; backfill required"
            )
        events: list[CapitalEvent] = []
        for signature in reversed(signatures):
            tx = await self._rpc(
                "getTransaction",
                [
                    signature,
                    {
                        "encoding": "jsonParsed",
                        "commitment": "finalized",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            )
            if not tx:
                raise RuntimeError("Solana transaction unavailable; cursor not advanced")
            meta = tx.get("meta") or {}
            if meta.get("err") is not None:
                continue
            message = (tx.get("transaction") or {}).get("message") or {}
            keys = [self._account_key(key) for key in message.get("accountKeys", [])]
            stamp = datetime.fromtimestamp(
                tx.get("blockTime") or observed_at.timestamp(), timezone.utc
            )
            slot = tx.get("slot")
            balance_deltas: dict[str, tuple[int, int]] = {}
            for field in ("preTokenBalances", "postTokenBalances"):
                for balance in meta.get(field) or []:
                    owner = balance.get("owner")
                    mint = balance.get("mint")
                    index = balance.get("accountIndex")
                    amount = (balance.get("uiTokenAmount") or {}).get("amount")
                    if owner != address or not mint or amount is None:
                        continue
                    previous_amount, next_amount = balance_deltas.get(mint, (0, 0))
                    raw = int(amount)
                    balance_deltas[mint] = (
                        raw if field == "preTokenBalances" else previous_amount,
                        raw if field == "postTokenBalances" else next_amount,
                    )
            for mint, (before_amount, after_amount) in balance_deltas.items():
                delta = after_amount - before_amount
                if delta == 0:
                    continue
                event_id = hashlib.sha256(
                    f"solana|{address}|{signature}|{mint}".encode()
                ).hexdigest()
                events.append(
                    CapitalEvent(
                        event_id,
                        "solana-rpc",
                        "token_balance_delta",
                        "crypto_spot",
                        "solana",
                        mint,
                        "spl_token_balance_change",
                        stamp,
                        observed_at,
                        float(abs(delta)),
                        "token_base_units",
                        None,
                        address,
                        transaction_id=signature,
                        block_number=int(slot) if slot is not None else None,
                        finality="finalized",
                        metadata={
                            "flow_sign": 1 if delta > 0 else -1,
                            "raw_delta": str(delta),
                            "decimals_required": True,
                        },
                    )
                )
            pre = meta.get("preBalances") or []
            post = meta.get("postBalances") or []
            try:
                account_index = keys.index(address)
            except ValueError:
                account_index = -1
            if 0 <= account_index < min(len(pre), len(post)):
                lamport_delta = int(post[account_index]) - int(pre[account_index])
                if lamport_delta:
                    event_id = hashlib.sha256(
                        f"solana|{address}|{signature}|SOL".encode()
                    ).hexdigest()
                    events.append(
                        CapitalEvent(
                            event_id,
                            "solana-rpc",
                            "native_balance_delta",
                            "crypto_spot",
                            "solana",
                            "SOL",
                            "sol_balance_change",
                            stamp,
                            observed_at,
                            float(abs(lamport_delta)),
                            "lamports",
                            None,
                            address,
                            transaction_id=signature,
                            block_number=int(slot) if slot is not None else None,
                            finality="finalized",
                            metadata={
                                "flow_sign": 1 if lamport_delta > 0 else -1,
                                "raw_delta": str(lamport_delta),
                                "decimals": 9,
                            },
                        )
                    )
        return events, newest or previous

    async def poll(
        self, *, cursor: str | None = None
    ) -> tuple[list[CapitalEvent], str | None]:
        cursors = self._cursor(cursor)
        now = datetime.now(timezone.utc)
        events: list[CapitalEvent] = []
        next_cursors = dict(cursors)
        for address in self.addresses:
            found, newest = await self._address_events(
                address, cursors.get(address), now
            )
            events.extend(found)
            if newest:
                next_cursors[address] = newest
        serialized = json.dumps(next_cursors, sort_keys=True, separators=(",", ":"))
        return events, serialized
