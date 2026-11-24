import json
import sqlite3
import time

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
    with pytest.raises(WebAuthnCredentialError, match="registration response missing"):
        rp.verify_registration(user_id="operator-1", credential={}, now=101)


def test_assertion_requires_registered_credential_and_signature(tmp_path):
    rp = make_rp(tmp_path)
    assertion = json.dumps({"id": "not-registered"}).encode()
    db = rp._connect()
    try:
        db.execute(
            "INSERT INTO webauthn_authentication_challenges"
            "(challenge,user_id,expires_at) VALUES(?,?,?)",
            ("Y2hhbGxlbmdl", "operator-1", int(time.time()) + 120),
        )
    finally:
        db.close()
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


def test_authentication_options_issue_user_bound_persisted_challenge(tmp_path):
    rp = make_rp(tmp_path)
    db = rp._connect()
    try:
        db.execute(
            "INSERT INTO webauthn_credentials "
            "(credential_id,user_id,public_key,sign_count,transports_json,"
            "device_type,backed_up,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                "Y3JlZGVudGlhbA",
                "operator-1",
                b"test-public-key",
                0,
                "[]",
                "single_device",
                0,
                100,
            ),
        )
    finally:
        db.close()

    options = rp.authentication_options(user_id="operator-1", now=100)
    assert options["rpId"] == "console.example"
    assert options["userVerification"] == "required"
    db = rp._connect()
    try:
        row = db.execute(
            "SELECT user_id,expires_at,consumed_at "
            "FROM webauthn_authentication_challenges WHERE challenge=?",
            (options["challenge"],),
        ).fetchone()
    finally:
        db.close()
    assert row == ("operator-1", 280, None)


def test_credential_inventory_and_scoped_offline_revocation(tmp_path):
    rp = make_rp(tmp_path)
    db = rp._connect()
    try:
        db.execute(
            "INSERT INTO webauthn_credentials "
            "(credential_id,user_id,public_key,sign_count,transports_json,"
            "device_type,backed_up,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                "Y3JlZGVudGlhbC1h",
                "operator-1",
                b"public-key-a",
                0,
                '["usb"]',
                "single_device",
                0,
                100,
            ),
        )
        db.execute(
            "INSERT INTO webauthn_credentials "
            "(credential_id,user_id,public_key,sign_count,transports_json,"
            "device_type,backed_up,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                "Y3JlZGVudGlhbC1i",
                "operator-2",
                b"public-key-b",
                0,
                "[]",
                "multi_device",
                1,
                101,
            ),
        )
    finally:
        db.close()

    inventory = rp.list_credentials(user_id="operator-1")
    assert len(inventory) == 1
    assert inventory[0]["credential_id"] == "Y3JlZGVudGlhbC1h"
    assert inventory[0]["transports"] == ["usb"]
    assert rp.revoke_credential(
        user_id="operator-1", credential_id="Y3JlZGVudGlhbC1b"
    ) is False
    assert rp.revoke_credential(
        user_id="operator-1", credential_id="Y3JlZGVudGlhbC1h"
    ) is True
    assert rp.credential_count(user_id="operator-1") == 0
    assert rp.credential_count(user_id="operator-2") == 1
    db = rp._connect()
    try:
        audit = db.execute(
            "SELECT event,user_id,credential_id FROM webauthn_credential_audit"
        ).fetchall()
        assert audit == [
            ("CREDENTIAL_REVOKED", "operator-1", "Y3JlZGVudGlhbC1h")
        ]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM webauthn_credential_audit")
    finally:
        db.close()


def test_remote_enrollment_bootstrap_is_single_use_and_persistent(tmp_path):
    rp = make_rp(tmp_path)
    token = "high-entropy-bootstrap-token-with-more-than-32-bytes"
    assert rp.consume_enrollment_bootstrap_token(
        user_id="operator-1",
        configured_token=token,
        supplied_token=token,
        now=100,
    )
    assert not rp.consume_enrollment_bootstrap_token(
        user_id="operator-1",
        configured_token=token,
        supplied_token=token,
        now=101,
    )

    restarted = make_rp(tmp_path)
    assert not restarted.consume_enrollment_bootstrap_token(
        user_id="operator-1",
        configured_token=token,
        supplied_token=token,
        now=102,
    )
    replacement = "replacement-bootstrap-token-with-over-32-bytes"
    restarted.reset_enrollment_bootstrap_token(
        user_id="operator-1",
        new_token=replacement,
        now=103,
    )
    assert restarted.consume_enrollment_bootstrap_token(
        user_id="operator-1",
        configured_token=replacement,
        supplied_token=replacement,
        now=104,
    )

    db = restarted._connect()
    try:
        events = db.execute(
            "SELECT event FROM webauthn_credential_audit ORDER BY sequence"
        ).fetchall()
    finally:
        db.close()
    assert events == [
        ("BOOTSTRAP_TOKEN_CONSUMED",),
        ("BOOTSTRAP_TOKEN_RESET",),
        ("BOOTSTRAP_TOKEN_CONSUMED",),
    ]
