"""Opt-in, deduplicated webhook alerts for explicitly watched CFEI addresses."""

from __future__ import annotations

import hashlib
import secrets
from typing import Any
from urllib.parse import urlsplit

import aiohttp


class CapitalFlowWebhookAlerter:
    """Send neutral observation notices; never describe a watchlist hit as a signal."""

    def __init__(
        self,
        *,
        redis_client: Any,
        watchlist: Any,
        webhook_url: str,
        timeout_seconds: float = 8.0,
        dedupe_ttl_seconds: int = 86_400,
        session: aiohttp.ClientSession | None = None,
    ):
        parsed = urlsplit(webhook_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or len(webhook_url) > 2048
        ):
            raise ValueError("alert webhook must be a valid HTTPS URL")
        if not 0.2 <= timeout_seconds <= 30:
            raise ValueError("invalid webhook timeout")
        if not 60 <= dedupe_ttl_seconds <= 604_800:
            raise ValueError("dedupe TTL must be between 60 seconds and 7 days")
        self.redis = redis_client
        self.watchlist = watchlist
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds
        self.dedupe_ttl_seconds = dedupe_ttl_seconds
        self.session = session
        self.owns_session = session is None

    async def close(self) -> None:
        if self.owns_session and self.session and not self.session.closed:
            await self.session.close()

    async def notify_event(self, event: Any) -> int:
        metadata = dict(getattr(event, "metadata", {}) or {})
        identity_kind = metadata.get("actor_identity_kind")
        if (
            identity_kind not in {"evm_address", "solana_address"}
            or metadata.get("aggregate_only") is True
            or metadata.get("actor_is_pool_caller_not_verified_beneficial_owner") is True
            or getattr(event, "finality", "") == "reverted"
        ):
            return 0

        matches = []
        for address in (
            getattr(event, "actor_id", None),
            getattr(event, "counterparty_id", None),
        ):
            if not address:
                continue
            entry = await self.watchlist.get(str(event.venue), str(address))
            if entry and entry["entity_id"] not in {
                item["entity_id"] for item in matches
            }:
                matches.append(entry)

        if not matches:
            return 0
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds)
            )

        sent = 0
        for entry in matches:
            fingerprint = hashlib.sha256(
                f"{event.event_id}|{entry['entity_id']}".encode()
            ).hexdigest()
            key = f"aitos:cfei:alert:{fingerprint}"
            token = secrets.token_urlsafe(18)
            acquired = await self.redis.set(key, token, nx=True, ex=300)
            if not acquired:
                continue
            payload = {
                "text": (
                    "AITOS CFEI observation (not a trading signal): "
                    f"{entry['entity_id']} appeared in {event.event_type}; "
                    f"event={event.event_id}; instrument={event.instrument}; "
                    f"quantity={event.quantity} {event.unit}; "
                    f"usd_value={event.usd_value}; "
                    f"observed_at={event.observed_time.isoformat()}"
                )
            }
            try:
                async with self.session.post(
                    self.webhook_url, json=payload
                ) as response:
                    response.raise_for_status()
                await self.redis.set(key, "sent", ex=self.dedupe_ttl_seconds)
                sent += 1
            except Exception:
                await self.redis.eval(
                    "if redis.call('GET', KEYS[1]) == ARGV[1] then "
                    "return redis.call('DEL', KEYS[1]) else return 0 end",
                    1,
                    key,
                    token,
                )
                raise
        return sent
