import json

import pytest

from aitos.security.webauthn_rp import (
    WebAuthnCredentialError,
    WebAuthnRelyingParty,
)


def make_rp(tmp_path):
    return WebAuthnRelyingParty(
        tmp_path / "credentials.sqlite",
        rp_id="console.example",
        rp_name="AITOS Console",
        allowed_origins={"https://console.example"},
    )


def test_registration_options_create_user_bound_one_time_challenge(tmp_path):
    rp = make_rp(tmp_path)
    options = rp.registration_options(user_id="operator-1", now=100)
    assert options["rp"]["id"] == "console.example"
    assert options["user"]["name"] == "operator-1"
    assert options["authenticatorSelection"]["userVerification"] == "required"

    db = rp._connect()
    try:
        row = db.execute(
            "SELECT user_id,expires_at,consumed_at "
            "FROM webauthn_registration_challenges WHERE challenge=?",
            (options["challenge"],),
        ).fetchone()
    finally:
        db.close()
    assert row == ("operator-1", 400, None)


def test_registration_requires_valid_cryptographic_attestation(tmp_path):
    rp = make_rp(tmp_path)
    with pytest.raises(WebAuthnCredentialError, match="clientDataJSON"):
        rp.verify_registration(user_id="operator-1", credential={}, now=101)


def test_assertion_requires_registered_credential_and_signature(tmp_path):
    rp = make_rp(tmp_path)
    assertion = json.dumps({"id": "not-registered"}).encode()
    with pytest.raises(WebAuthnCredentialError, match="unknown WebAuthn credential"):
        rp.verify_assertion(
            assertion=assertion,
            challenge="Y2hhbGxlbmdl",
            expected_origin={"https://console.example"},
            expected_rp_id="console.example",
            require_user_verification=True,
        )


def test_verifier_rejects_wrong_rp_or_origin_context(tmp_path):
    rp = make_rp(tmp_path)
    with pytest.raises(WebAuthnCredentialError, match="context mismatch"):
        rp.verify_assertion(
            assertion=b"{}",
            challenge="Y2hhbGxlbmdl",
            expected_origin={"https://evil.example"},
            expected_rp_id="console.example",
            require_user_verification=True,
        )
