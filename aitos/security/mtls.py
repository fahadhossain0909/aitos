"""Fail-closed TLS 1.3 mutual-authentication context builders.

These helpers establish transport identity only. They do not authorize intents,
provide a network server, or replace signer-side policy and replay controls.
"""
from __future__ import annotations

import re
import ssl


class MutualTLSConfigurationError(ValueError):
    """TLS material is missing or does not satisfy the transport policy."""


def _readable_file(path: str) -> str:
    if type(path) is not str or not path or "\x00" in path:
        raise MutualTLSConfigurationError("tls_file_path_required")
    return path


def build_server_context(
    *,
    certificate_chain: str,
    private_key: str,
    trusted_client_ca: str,
) -> ssl.SSLContext:
    """Build a server context that requires a trusted client certificate."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    try:
        context.load_cert_chain(
            _readable_file(certificate_chain),
            _readable_file(private_key),
        )
        context.load_verify_locations(cafile=_readable_file(trusted_client_ca))
    except (OSError, ssl.SSLError) as exc:
        raise MutualTLSConfigurationError("mtls_server_material_invalid") from exc
    return context


def build_client_context(
    *,
    certificate_chain: str,
    private_key: str,
    trusted_server_ca: str,
) -> ssl.SSLContext:
    """Build a client context that verifies the signer/server certificate."""
    context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH,
        cafile=_readable_file(trusted_server_ca),
    )
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    try:
        context.load_cert_chain(
            _readable_file(certificate_chain),
            _readable_file(private_key),
        )
    except (OSError, ssl.SSLError) as exc:
        raise MutualTLSConfigurationError("mtls_client_material_invalid") from exc
    return context


def require_client_uri_identity(
    peer_certificate: dict,
    *,
    allowed_uris: frozenset[str],
) -> str:
    """Map an already TLS-verified peer cert to one pinned URI SAN identity."""
    if (
        not isinstance(peer_certificate, dict)
        or not isinstance(allowed_uris, frozenset)
        or not allowed_uris
    ):
        raise MutualTLSConfigurationError("client_identity_allowlist_required")
    if any(
        type(uri) is not str
        or not re.fullmatch(r"spiffe://[A-Za-z0-9._/-]{1,200}", uri)
        for uri in allowed_uris
    ):
        raise MutualTLSConfigurationError("invalid_client_identity_allowlist")
    identities = {
        value
        for kind, value in peer_certificate.get("subjectAltName", ())
        if kind == "URI" and isinstance(value, str)
    }
    matched = identities.intersection(allowed_uris)
    if len(matched) != 1:
        raise MutualTLSConfigurationError("client_identity_not_pinned")
    return next(iter(matched))
