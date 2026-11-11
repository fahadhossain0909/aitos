import json

import pytest

from aitos.security.signer_rpc_protocol import (
    ClientAuthorizationRegistry,
    ClientGrant,
    SignerProtocolRejected,
    parse_signer_request,
)

IDENTITY = "spiffe://aitos/runtime/paper-01"
GRANT = ClientGrant(
    account_alias="paper-primary",
    allowed_symbols=frozenset({"BTCUSDT"}),
    allowed_serials=frozenset({"A1B2"}),
)
CERT = {"subjectAltName": [("URI", IDENTITY)], "serialNumber": "A1B2"}


def request(**overrides):
    value = {
        "protocol_version": 1,
        "request_id": "req-001",
        "sent_at_ms": 1_800_000_000_000,
        "account_alias": "paper-primary",
        "intent": {
            "account_alias": "paper-primary",
            "symbol": "BTCUSDT",
            "nonce": "AAECAwQFBgcICQoLDA0ODw",
        },
    }
    value.update(overrides)
    return json.dumps(value)


def test_certificate_identity_serial_and_account_scope_are_authorized():
    registry = ClientAuthorizationRegistry({IDENTITY: GRANT})
    identity, grant = registry.authorize(CERT, now_ms=1_800_000_000_000)
    assert identity == IDENTITY
    assert grant.account_alias == "paper-primary"


@pytest.mark.parametrize(
    "cert",
    [
        {"subjectAltName": [("URI", IDENTITY)], "serialNumber": "FFFF"},
        {"subjectAltName": [("URI", IDENTITY)], "serialNumber": "A1B2"},
    ],
)
def test_certificate_revocation_and_unknown_serial_fail_closed(cert):
    registry = ClientAuthorizationRegistry(
        {
            IDENTITY: ClientGrant(
                account_alias="paper-primary",
                allowed_symbols=frozenset({"BTCUSDT"}),
                allowed_serials=frozenset({"A1B2"}),
                revoked_serials=frozenset({"A1B2"}) if cert["serialNumber"] == "A1B2" else frozenset(),
            )
        }
    )
    with pytest.raises(SignerProtocolRejected, match="client_certificate_not_authorized"):
        registry.authorize(cert, now_ms=1_800_000_000_000)


def test_certificate_validity_window_is_enforced():
    registry = ClientAuthorizationRegistry(
        {
            IDENTITY: ClientGrant(
                account_alias="paper-primary",
                allowed_symbols=frozenset({"BTCUSDT"}),
                allowed_serials=frozenset({"A1B2"}),
                valid_from_ms=100,
                valid_until_ms=200,
            )
        }
    )
    with pytest.raises(SignerProtocolRejected, match="client_certificate_not_authorized"):
        registry.authorize(CERT, now_ms=200)


def test_bounded_request_parses_and_freezes_intent():
    parsed = parse_signer_request(
        request(),
        authenticated_identity=IDENTITY,
        grant=GRANT,
        now_ms=1_800_000_000_000,
    )
    assert parsed.request_id == "req-001"
    assert parsed.intent["symbol"] == "BTCUSDT"
    with pytest.raises(TypeError):
        parsed.intent["symbol"] = "ETHUSDT"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"account_alias": "other"}, "client_account_scope_mismatch"),
        (
            {
                "intent": {
                    "account_alias": "paper-primary",
                    "symbol": "ETHUSDT",
                    "nonce": "AAECAwQFBgcICQoLDA0ODw",
                }
            },
            "client_symbol_scope_denied",
        ),
        ({"sent_at_ms": 1}, "signer_request_clock_skew"),
        ({"protocol_version": 2}, "unsupported_signer_protocol_version"),
    ],
)
def test_request_scope_and_freshness_fail_closed(overrides, reason):
    with pytest.raises(SignerProtocolRejected, match=reason):
        parse_signer_request(
            request(**overrides),
            authenticated_identity=IDENTITY,
            grant=GRANT,
            now_ms=1_800_000_000_000,
        )


def test_credentials_and_reusable_signatures_are_never_accepted():
    body = {
        "protocol_version": 1,
        "request_id": "req-002",
        "sent_at_ms": 1_800_000_000_000,
        "account_alias": "paper-primary",
        "intent": {
            "account_alias": "paper-primary",
            "symbol": "BTCUSDT",
            "api_secret": "never-send-this",
        },
    }
    with pytest.raises(SignerProtocolRejected, match="credential_or_signature_field_forbidden"):
        parse_signer_request(
            json.dumps(body),
            authenticated_identity=IDENTITY,
            grant=GRANT,
            now_ms=1_800_000_000_000,
        )


def test_duplicate_json_keys_and_oversized_body_are_rejected():
    duplicate = '{"protocol_version":1,"protocol_version":1}'
    with pytest.raises(SignerProtocolRejected, match="duplicate_json_key"):
        parse_signer_request(
            duplicate,
            authenticated_identity=IDENTITY,
            grant=GRANT,
            now_ms=1_800_000_000_000,
        )
    with pytest.raises(SignerProtocolRejected, match="intent_body_too_large"):
        parse_signer_request(
            " " * 20_000,
            authenticated_identity=IDENTITY,
            grant=GRANT,
            now_ms=1_800_000_000_000,
        )
