import pytest

from aitos.security.mtls import (
    MutualTLSConfigurationError,
    build_client_context,
    build_server_context,
    require_client_uri_identity,
)


IDENTITY = "spiffe://aitos/signer/client-01"


def test_client_identity_requires_exact_pinned_uri_san():
    cert = {"subjectAltName": [("URI", IDENTITY), ("DNS", "client.local")]}
    assert require_client_uri_identity(cert, allowed_uris=frozenset({IDENTITY})) == IDENTITY


@pytest.mark.parametrize(
    ("cert", "allowlist", "reason"),
    [
        ({}, frozenset({IDENTITY}), "client_identity_not_pinned"),
        ({"subjectAltName": [("URI", IDENTITY)]}, frozenset(), "client_identity_allowlist_required"),
        ({"subjectAltName": [("URI", IDENTITY), ("URI", "spiffe://aitos/signer/other")]}, frozenset({IDENTITY, "spiffe://aitos/signer/other"}), "client_identity_not_pinned"),
        ({"subjectAltName": [("DNS", IDENTITY)]}, frozenset({IDENTITY}), "client_identity_not_pinned"),
    ],
)
def test_client_identity_fails_closed(cert, allowlist, reason):
    with pytest.raises(MutualTLSConfigurationError, match=reason):
        require_client_uri_identity(cert, allowed_uris=allowlist)


def test_server_and_client_tls_contexts_reject_missing_material(tmp_path):
    missing = str(tmp_path / "missing.pem")
    with pytest.raises(MutualTLSConfigurationError, match="mtls_server_material_invalid"):
        build_server_context(
            certificate_chain=missing,
            private_key=missing,
            trusted_client_ca=missing,
        )
    with pytest.raises((MutualTLSConfigurationError, OSError, ValueError)):
        build_client_context(
            certificate_chain=missing,
            private_key=missing,
            trusted_server_ca=missing,
        )
