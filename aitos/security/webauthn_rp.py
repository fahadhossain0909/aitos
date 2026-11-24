"""Cryptographic WebAuthn RP ceremonies and persistent credential registry.

This adapter uses py_webauthn for client-data, origin, RP-ID hash, authenticator
flags, attestation and signature verification. Credential counters are updated
under SQLite BEGIN IMMEDIATE so concurrent assertions cannot both consume the
same authenticator counter.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from webauthn import (
    base64url_to_bytes,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    UserVerificationRequirement,
)


class WebAuthnCredentialError(PermissionError):
    """Registration or authentication failed closed."""


class WebAuthnRelyingParty:
    def __init__(
        self,
        path: str | Path,
        *,
        rp_id: str,
        rp_name: str,
        allowed_origins: set[str],
        timeout_ms: int = 60_000,
    ) -> None:
        if (
            not rp_id
            or not rp_name
            or not allowed_origins
            or not 5_000 <= timeout_ms <= 120_000
        ):
            raise ValueError(
                "explicit RP identity, origins and bounded timeout required"
            )
        self.path = Path(path)
        self.rp_id = rp_id
        self.rp_name = rp_name
        self.origins = frozenset(allowed_origins)
        self.timeout_ms = timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS webauthn_credentials (
                    credential_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    public_key BLOB NOT NULL,
                    sign_count INTEGER NOT NULL CHECK(sign_count >= 0),
                    transports_json TEXT NOT NULL DEFAULT '[]',
                    device_type TEXT NOT NULL,
                    backed_up INTEGER NOT NULL CHECK(backed_up IN (0,1)),
                    created_at INTEGER NOT NULL,
                    last_used_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_webauthn_credentials_user
                    ON webauthn_credentials(user_id);
                CREATE TABLE IF NOT EXISTS webauthn_registration_challenges (
                    challenge TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    consumed_at INTEGER
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @staticmethod
    def _user_id(user_id: str) -> str:
        if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 256:
            raise ValueError("bounded authenticated user identity is required")
        return user_id

    def registration_options(
        self, *, user_id: str, now: int, ttl_seconds: int = 300
    ) -> dict[str, Any]:
        user = self._user_id(user_id)
        if not 30 <= ttl_seconds <= 600:
            raise ValueError("registration challenge TTL must be 30..600 seconds")
        db = self._connect()
        try:
            existing = db.execute(
                "SELECT credential_id FROM webauthn_credentials WHERE user_id=?",
                (user,),
            ).fetchall()
        finally:
            db.close()
        options = generate_registration_options(
            rp_id=self.rp_id,
            rp_name=self.rp_name,
            user_id=hashlib.sha256(user.encode("utf-8")).digest(),
            user_name=user,
            user_display_name=user,
            timeout=self.timeout_ms,
            authenticator_selection=AuthenticatorSelectionCriteria(
                user_verification=UserVerificationRequirement.REQUIRED
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(row[0]))
                for row in existing
            ],
        )
        serialized = json.loads(options_to_json(options))
        challenge = serialized["challenge"]
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO webauthn_registration_challenges"
                "(challenge,user_id,expires_at) VALUES(?,?,?)",
                (challenge, user, int(now) + ttl_seconds),
            )
            db.commit()
        finally:
            db.close()
        return serialized

    def verify_registration(
        self, *, user_id: str, credential: dict[str, Any], now: int
    ) -> str:
        user = self._user_id(user_id)
        if not isinstance(credential, dict):
            raise WebAuthnCredentialError("registration credential must be an object")
        response = credential.get("response")
        if not isinstance(response, dict):
            raise WebAuthnCredentialError("registration response missing")
        client_data = response.get("clientDataJSON")
        if not isinstance(client_data, str):
            raise WebAuthnCredentialError("registration clientDataJSON missing")
        try:
            client_json = json.loads(base64url_to_bytes(client_data))
            challenge = client_json["challenge"]
            verification = verify_registration_response(
                credential=credential,
                expected_challenge=base64url_to_bytes(challenge),
                expected_rp_id=self.rp_id,
                expected_origin=list(self.origins),
                require_user_verification=True,
            )
        except Exception as exc:
            raise WebAuthnCredentialError(
                "cryptographic WebAuthn registration failed"
            ) from exc

        credential_id = verification.credential_id
        from webauthn.helpers import bytes_to_base64url

        credential_key = bytes_to_base64url(credential_id)
        transports = response.get("transports", [])
        if not isinstance(transports, list) or any(
            not isinstance(item, str) for item in transports
        ):
            raise WebAuthnCredentialError("invalid authenticator transport metadata")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            challenge_row = db.execute(
                "SELECT user_id,expires_at,consumed_at "
                "FROM webauthn_registration_challenges WHERE challenge=?",
                (challenge,),
            ).fetchone()
            if (
                challenge_row is None
                or challenge_row[0] != user
                or challenge_row[1] <= int(now)
                or challenge_row[2] is not None
            ):
                db.rollback()
                raise WebAuthnCredentialError(
                    "registration challenge missing, expired or already consumed"
                )
            db.execute(
                "INSERT INTO webauthn_credentials "
                "(credential_id,user_id,public_key,sign_count,transports_json,"
                "device_type,backed_up,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    credential_key,
                    user,
                    verification.credential_public_key,
                    int(verification.sign_count),
                    json.dumps(transports, separators=(",", ":")),
                    str(verification.credential_device_type),
                    int(bool(verification.credential_backed_up)),
                    int(now),
                ),
            )
            db.execute(
                "UPDATE webauthn_registration_challenges SET consumed_at=? "
                "WHERE challenge=? AND consumed_at IS NULL",
                (int(now), challenge),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return credential_key

    def verify_assertion(
        self,
        *,
        assertion: bytes,
        challenge: str,
        expected_origin: set[str] | frozenset[str],
        expected_rp_id: str,
        require_user_verification: bool = True,
    ) -> dict[str, Any]:
        if (
            expected_rp_id != self.rp_id
            or frozenset(expected_origin) != self.origins
            or not require_user_verification
            or not isinstance(assertion, bytes)
            or not assertion
            or len(assertion) > 65_536
        ):
            raise WebAuthnCredentialError("WebAuthn verifier context mismatch")
        try:
            credential = json.loads(assertion)
            if not isinstance(credential, dict):
                raise ValueError("credential must be an object")
            from webauthn.helpers import bytes_to_base64url

            credential_id = credential["id"]
            if not isinstance(credential_id, str):
                raise ValueError("credential ID missing")
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT user_id,public_key,sign_count FROM webauthn_credentials "
                    "WHERE credential_id=?",
                    (credential_id,),
                ).fetchone()
                if row is None:
                    raise WebAuthnCredentialError("unknown WebAuthn credential")
                user_id, public_key, current_count = row
                verified = verify_authentication_response(
                    credential=credential,
                    expected_challenge=base64url_to_bytes(challenge),
                    expected_rp_id=self.rp_id,
                    expected_origin=list(self.origins),
                    credential_public_key=bytes(public_key),
                    credential_current_sign_count=int(current_count),
                    require_user_verification=True,
                )
                if not verified.user_verified:
                    raise WebAuthnCredentialError(
                        "authenticator did not verify the user"
                    )
                changed = db.execute(
                    "UPDATE webauthn_credentials SET sign_count=?,last_used_at=strftime('%s','now') "
                    "WHERE credential_id=? AND sign_count=?",
                    (
                        int(verified.new_sign_count),
                        credential_id,
                        int(current_count),
                    ),
                ).rowcount
                if changed != 1:
                    raise WebAuthnCredentialError(
                        "credential counter changed concurrently"
                    )
                db.commit()
                return {
                    "verified": True,
                    "user_id": user_id,
                    "user_verified": True,
                    "origin": json.loads(
                        base64url_to_bytes(credential["response"]["clientDataJSON"])
                    )["origin"],
                    "rp_id": self.rp_id,
                    "credential_id": bytes_to_base64url(verified.credential_id),
                    "new_sign_count": int(verified.new_sign_count),
                }
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
        except WebAuthnCredentialError:
            raise
        except Exception as exc:
            raise WebAuthnCredentialError(
                "cryptographic WebAuthn assertion failed"
            ) from exc
