"""Bounded, identity-scoped signer RPC request contract (no network listener).

This module validates requests only after a TLS stack has verified the peer
certificate. It never signs, dispatches, reads credentials, or opens sockets.
Nonce/idempotency consumption remains the responsibility of the durable
paper/signer processor after all authentication and policy checks pass.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from aitos.security.mtls import (
    MutualTLSConfigurationError,
    require_client_uri_identity,
)
from aitos.security.signing_policy import IntentRejected, parse_order_intent_json


class SignerProtocolRejected(ValueError):
    """A remote request failed the signer transport contract."""


_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SERIAL = re.compile(r"[0-9A-Fa-f]{1,64}\Z")
_ALLOWED_ENVELOPE_FIELDS = frozenset(
    {"protocol_version", "request_id", "sent_at_ms", "account_alias", "intent"}
)


@dataclass(frozen=True)
class ClientGrant:
    """One certificate identity's least-privilege account grant.

    Multiple serials may be allowed during a controlled certificate rotation.
    Remove the old serial after the new certificate has been deployed and
    verified. A revoked serial always overrides the allowlist.
    """

    identity: str
    account_alias: str
    allowed_symbols: frozenset[str]
    allowed_serials: frozenset[str]
    revoked_serials: frozenset[str] = frozenset()
    enabled: bool = True
    valid_from_ms: int = 0
    valid_until_ms: int = 9_223_372_036_854_775_807
    serial_validity: Mapping[str, tuple[int, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.identity, str) or not self.identity.startswith(
            "spiffe://"
        ):
            raise ValueError("client_grant_identity_required")
        if not isinstance(self.account_alias, str) or not self.account_alias:
            raise ValueError("client_grant_account_required")
        if (
            not isinstance(self.allowed_symbols, frozenset)
            or not self.allowed_symbols
            or any(
                not isinstance(item, str) or not item for item in self.allowed_symbols
            )
        ):
            raise ValueError("client_grant_symbols_required")
        if (
            not isinstance(self.allowed_serials, frozenset)
            or not self.allowed_serials
            or any(
                not isinstance(item, str) or not _SERIAL.fullmatch(item)
                for item in self.allowed_serials
            )
        ):
            raise ValueError("client_grant_serials_required")
        if not isinstance(self.revoked_serials, frozenset) or any(
            not isinstance(item, str) or not _SERIAL.fullmatch(item)
            for item in self.revoked_serials
        ):
            raise ValueError("client_grant_revocations_invalid")
        if type(self.enabled) is not bool:
            raise ValueError("client_grant_enabled_flag_required")
        if (
            isinstance(self.valid_from_ms, bool)
            or not isinstance(self.valid_from_ms, int)
            or isinstance(self.valid_until_ms, bool)
            or not isinstance(self.valid_until_ms, int)
            or self.valid_from_ms < 0
            or self.valid_until_ms <= self.valid_from_ms
        ):
            raise ValueError("client_grant_validity_invalid")
        if not isinstance(self.serial_validity, Mapping):
            raise ValueError("client_grant_serial_validity_invalid")
        validity = {}
        for serial, window in self.serial_validity.items():
            if (
                not isinstance(serial, str)
                or not _SERIAL.fullmatch(serial)
                or serial not in self.allowed_serials
                or type(window) not in (tuple, list)
                or len(window) != 2
                or type(window[0]) is not int
                or type(window[1]) is not int
                or window[0] < 0
                or window[1] <= window[0]
            ):
                raise ValueError("client_grant_serial_validity_invalid")
            validity[serial] = (window[0], window[1])
        object.__setattr__(self, "serial_validity", MappingProxyType(validity))


@dataclass(frozen=True)
class SignerRequest:
    authenticated_identity: str
    request_id: str
    sent_at_ms: int
    account_alias: str
    intent: Mapping[str, object]


class ClientAuthorizationRegistry:
    """Immutable identity-to-account/symbol authorization snapshot."""

    def __init__(self, grants: Mapping[str, ClientGrant]) -> None:
        if not isinstance(grants, Mapping) or not grants:
            raise ValueError("client_grants_required")
        normalized = dict(grants)
        if any(
            not isinstance(identity, str)
            or not identity.startswith("spiffe://")
            or not isinstance(grant, ClientGrant)
            or identity != grant.identity
            for identity, grant in normalized.items()
        ):
            raise ValueError("client_grant_mapping_invalid")
        self._grants = MappingProxyType(normalized)

    def authorize(
        self,
        peer_certificate: Mapping[str, object],
        *,
        now_ms: int,
    ) -> tuple[str, ClientGrant]:
        """Authorize TLS-verified certificate identity and serial.

        The caller must obtain peer_certificate from an established TLS
        connection using build_server_context (CERT_REQUIRED + trusted CA).
        """
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
            raise SignerProtocolRejected("invalid_signer_clock")
        try:
            identity = require_client_uri_identity(
                dict(peer_certificate), allowed_uris=frozenset(self._grants)
            )
        except MutualTLSConfigurationError as exc:
            raise SignerProtocolRejected(
                "client_certificate_identity_rejected"
            ) from exc
        grant = self._grants[identity]
        serial = peer_certificate.get("serialNumber")
        if not isinstance(serial, str) or not _SERIAL.fullmatch(serial):
            raise SignerProtocolRejected("client_certificate_serial_missing")
        serial = serial.upper()
        serial_window = grant.serial_validity.get(
            serial, (grant.valid_from_ms, grant.valid_until_ms)
        )
        if (
            not grant.enabled
            or serial in grant.revoked_serials
            or serial not in grant.allowed_serials
            or not serial_window[0] <= now_ms < serial_window[1]
        ):
            raise SignerProtocolRejected("client_certificate_not_authorized")
        return identity, grant


def parse_signer_request(
    raw: str | bytes,
    *,
    authenticated_identity: str,
    grant: ClientGrant,
    now_ms: int,
    max_clock_skew_ms: int = 5_000,
    max_body_bytes: int = 16_384,
) -> SignerRequest:
    """Parse a bounded request and enforce the certificate's account/symbol scope."""
    try:
        envelope = parse_order_intent_json(raw, max_bytes=max_body_bytes)
    except IntentRejected as exc:
        raise SignerProtocolRejected(str(exc)) from exc
    if set(envelope) != _ALLOWED_ENVELOPE_FIELDS:
        raise SignerProtocolRejected("signer_request_fields_invalid")
    if (
        envelope.get("protocol_version") != 1
        or type(envelope.get("protocol_version")) is not int
    ):
        raise SignerProtocolRejected("unsupported_signer_protocol_version")
    request_id = envelope.get("request_id")
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise SignerProtocolRejected("invalid_signer_request_id")
    sent_at_ms = envelope.get("sent_at_ms")
    if (
        isinstance(sent_at_ms, bool)
        or not isinstance(sent_at_ms, int)
        or sent_at_ms < 0
    ):
        raise SignerProtocolRejected("invalid_signer_request_timestamp")
    if (
        isinstance(now_ms, bool)
        or not isinstance(now_ms, int)
        or now_ms < 0
        or isinstance(max_clock_skew_ms, bool)
        or not isinstance(max_clock_skew_ms, int)
        or not 0 <= max_clock_skew_ms <= 60_000
        or abs(now_ms - sent_at_ms) > max_clock_skew_ms
    ):
        raise SignerProtocolRejected("signer_request_clock_skew")
    if envelope.get("account_alias") != grant.account_alias:
        raise SignerProtocolRejected("client_account_scope_mismatch")
    intent = envelope.get("intent")
    if not isinstance(intent, dict):
        raise SignerProtocolRejected("signer_intent_object_required")
    if intent.get("account_alias") != grant.account_alias:
        raise SignerProtocolRejected("intent_account_scope_mismatch")
    symbol = intent.get("symbol")
    if not isinstance(symbol, str) or symbol not in grant.allowed_symbols:
        raise SignerProtocolRejected("client_symbol_scope_denied")
    # Credential/signature material is never part of this protocol envelope.
    forbidden = {"api_key", "api_secret", "private_key", "signature", "signed_request"}
    if forbidden.intersection(intent):
        raise SignerProtocolRejected("credential_or_signature_field_forbidden")
    if authenticated_identity != grant.identity:
        raise SignerProtocolRejected("authenticated_client_identity_mismatch")
    return SignerRequest(
        authenticated_identity=authenticated_identity,
        request_id=request_id,
        sent_at_ms=sent_at_ms,
        account_alias=grant.account_alias,
        intent=MappingProxyType(dict(intent)),
    )


def authenticate_signer_request(
    raw: str | bytes,
    *,
    peer_certificate: Mapping[str, object],
    registry: ClientAuthorizationRegistry,
    now_ms: int,
    max_clock_skew_ms: int = 5_000,
    max_body_bytes: int = 16_384,
) -> SignerRequest:
    """Authenticate from the TLS peer certificate, then validate its request.

    A future socket handler should pass only SSLSocket.getpeercert() from the
    accepted TLS connection. It must not construct this mapping from headers,
    JSON fields, forwarded client-certificate headers, or caller parameters.
    """
    identity, grant = registry.authorize(peer_certificate, now_ms=now_ms)
    return parse_signer_request(
        raw,
        authenticated_identity=identity,
        grant=grant,
        now_ms=now_ms,
        max_clock_skew_ms=max_clock_skew_ms,
        max_body_bytes=max_body_bytes,
    )
