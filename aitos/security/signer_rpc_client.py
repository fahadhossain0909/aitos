"""Bounded asyncio mTLS client for the remote paper signer."""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import uuid
from dataclasses import dataclass
from typing import Mapping

_OUTCOME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_STATUSES = frozenset({"accepted", "rejected", "unknown", "reserved", "dispatching"})


class SignerRPCClientError(RuntimeError):
    """The remote signer RPC could not be completed or safely interpreted."""


@dataclass(frozen=True)
class SignerRPCReceipt:
    request_id: str
    status: str
    outcome_code: str
    duplicate: bool


class SignerRPCClient:
    """Submit one bounded intent over TLS 1.3 mTLS; never retries automatically."""

    def __init__(
        self,
        *,
        ssl_context: ssl.SSLContext,
        host: str,
        port: int,
        server_hostname: str,
        max_request_bytes: int = 16_384,
        timeout_seconds: float = 5.0,
    ) -> None:
        if not isinstance(ssl_context, ssl.SSLContext):
            raise TypeError("signer_client_tls_context_required")
        if (
            ssl_context.protocol != ssl.PROTOCOL_TLS_CLIENT
            or ssl_context.verify_mode != ssl.CERT_REQUIRED
            or not ssl_context.check_hostname
            or ssl_context.minimum_version < ssl.TLSVersion.TLSv1_3
        ):
            raise ValueError("tls13_server_verification_required")
        if (
            type(host) is not str
            or not host
            or type(server_hostname) is not str
            or not server_hostname
        ):
            raise ValueError("signer_rpc_host_required")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid_signer_rpc_port")
        if type(max_request_bytes) is not int or not 256 <= max_request_bytes <= 65_536:
            raise ValueError("invalid_signer_rpc_body_limit")
        if not 0.1 <= timeout_seconds <= 30:
            raise ValueError("invalid_signer_rpc_timeout")
        self.ssl_context = ssl_context
        self.host = host
        self.port = port
        self.server_hostname = server_hostname
        self.max_request_bytes = max_request_bytes
        self.timeout_seconds = timeout_seconds

    async def submit(
        self,
        intent: Mapping[str, object],
        *,
        account_alias: str,
        sent_at_ms: int,
        request_id: str | None = None,
    ) -> SignerRPCReceipt:
        if (
            not isinstance(intent, Mapping)
            or not isinstance(account_alias, str)
            or not account_alias
        ):
            raise ValueError("signer_rpc_intent_and_account_required")
        if type(sent_at_ms) is not int or sent_at_ms < 0:
            raise ValueError("invalid_signer_rpc_timestamp")
        request_id = request_id or uuid.uuid4().hex
        if type(request_id) is not str or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", request_id
        ):
            raise ValueError("invalid_signer_rpc_request_id")
        envelope = {
            "protocol_version": 1,
            "request_id": request_id,
            "sent_at_ms": sent_at_ms,
            "account_alias": account_alias,
            "intent": dict(intent),
        }
        try:
            body = json.dumps(
                envelope,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise ValueError("signer_rpc_intent_not_json_safe") from None
        if len(body) > self.max_request_bytes:
            raise ValueError("signer_rpc_request_too_large")

        writer = None
        sent = False
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.host,
                    self.port,
                    ssl=self.ssl_context,
                    server_hostname=self.server_hostname,
                    ssl_handshake_timeout=min(5.0, self.timeout_seconds),
                    limit=4096,
                ),
                timeout=self.timeout_seconds,
            )
            writer.write(body + b"\n")
            sent = True
            await writer.drain()
            raw = await asyncio.wait_for(
                reader.readline(), timeout=self.timeout_seconds
            )
            if not raw or len(raw) > 4096 or not raw.endswith(b"\n"):
                raise SignerRPCClientError("signer_response_incomplete")
            try:
                response = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError, RecursionError):
                raise SignerRPCClientError("signer_response_invalid") from None
            if (
                type(response) is not dict
                or set(response) - {"status", "outcome_code", "duplicate"}
                or response.get("status") not in _STATUSES
                or type(response.get("outcome_code")) is not str
                or not _OUTCOME.fullmatch(response["outcome_code"])
                or ("duplicate" in response and type(response["duplicate"]) is not bool)
            ):
                raise SignerRPCClientError("signer_response_schema_invalid")
            return SignerRPCReceipt(
                request_id=request_id,
                status=response["status"],
                outcome_code=response["outcome_code"],
                duplicate=response.get("duplicate", False),
            )
        except asyncio.TimeoutError:
            if sent:
                raise SignerRPCClientError(
                    "signer_response_timeout_ambiguous"
                ) from None
            raise SignerRPCClientError("signer_connection_timeout") from None
        except (OSError, ssl.SSLError, asyncio.IncompleteReadError, ValueError):
            if sent:
                raise SignerRPCClientError(
                    "signer_transport_failure_ambiguous"
                ) from None
            raise SignerRPCClientError("signer_connection_unavailable") from None
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError, ssl.SSLError):
                    pass
