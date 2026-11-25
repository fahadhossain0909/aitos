"""Cryptographic WebAuthn RP ceremonies and persistent credential registry.

This adapter uses py_webauthn for client-data, origin, RP-ID hash, authenticator
flags, attestation and signature verification. Credential counters are updated
under SQLite BEGIN IMMEDIATE so concurrent assertions cannot both consume the
same authenticator counter.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any

from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
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
        self.path = Path(path).expanduser()
        if self.path.is_symlink():
            raise ValueError("WebAuthn credential database symlinks are forbidden")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.is_symlink():
            raise ValueError("WebAuthn credential directory symlinks are forbidden")
        if os.name != "nt" and stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077:
            raise ValueError("WebAuthn credential directory must have mode 0700")
        self.rp_id = rp_id
        self.rp_name = rp_name
        self.origins = frozenset(allowed_origins)
        self.timeout_ms = timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = self._connect()
        try:
            db.execute("PRAGMA journal_mode=WAL")
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
                CREATE TABLE IF NOT EXISTS webauthn_authentication_challenges (
                    challenge TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    consumed_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS webauthn_enrollment_bootstrap (
                    user_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL,
                    consumed_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS webauthn_credential_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL CHECK(event IN ('CREDENTIAL_REGISTERED','CREDENTIAL_REVOKED','BOOTSTRAP_TOKEN_CONSUMED','BOOTSTRAP_TOKEN_RESET')),
                    user_id TEXT NOT NULL,
                    credential_id TEXT NOT NULL,
                    recorded_at INTEGER NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS webauthn_credential_audit_no_update
                    BEFORE UPDATE ON webauthn_credential_audit BEGIN
                        SELECT RAISE(ABORT,'WebAuthn credential audit is append-only');
                    END;
                CREATE TRIGGER IF NOT EXISTS webauthn_credential_audit_no_delete
                    BEFORE DELETE ON webauthn_credential_audit BEGIN
                        SELECT RAISE(ABORT,'WebAuthn credential audit is append-only');
                    END;
                """
            )
        finally:
            db.close()
        if self.path.exists() and os.name != "nt":
            os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @staticmethod
    def _user_id(user_id: str) -> str:
        if (
            not isinstance(user_id, str)
            or not user_id.strip()
            or user_id != user_id.strip()
            or len(user_id) > 256
        ):
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
                "DELETE FROM webauthn_registration_challenges "
                "WHERE expires_at<=? OR consumed_at IS NOT NULL",
                (int(now),),
            )
            db.execute(
                "INSERT INTO webauthn_registration_challenges"
                "(challenge,user_id,expires_at) VALUES(?,?,?)",
                (challenge, user, int(now) + ttl_seconds),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return serialized

    def credential_count(self, *, user_id: str) -> int:
        """Return only the credential count for a validated local operator ID."""
        user = self._user_id(user_id)
        db = self._connect()
        try:
            row = db.execute(
                "SELECT count(*) FROM webauthn_credentials WHERE user_id=?",
                (user,),
            ).fetchone()
            return int(row[0])
        finally:
            db.close()

    def authentication_options(
        self, *, user_id: str, now: int, ttl_seconds: int = 180
    ) -> dict[str, Any]:
        """Issue a short-lived, user-bound, one-time assertion challenge."""
        user = self._user_id(user_id)
        if not 30 <= ttl_seconds <= 600:
            raise ValueError("authentication challenge TTL must be 30..600 seconds")
        db = self._connect()
        try:
            credentials = db.execute(
                "SELECT credential_id,transports_json FROM webauthn_credentials "
                "WHERE user_id=? ORDER BY created_at",
                (user,),
            ).fetchall()
        finally:
            db.close()
        if not credentials:
            raise WebAuthnCredentialError("user has no registered WebAuthn credential")
        descriptors = []
        for credential_id, transports_json in credentials:
            from webauthn.helpers.structs import AuthenticatorTransport

            transports = [
                AuthenticatorTransport(item)
                for item in json.loads(transports_json)
                if item in {transport.value for transport in AuthenticatorTransport}
            ]
            descriptors.append(
                PublicKeyCredentialDescriptor(
                    id=base64url_to_bytes(credential_id),
                    transports=transports or None,
                )
            )
        options = generate_authentication_options(
            rp_id=self.rp_id,
            timeout=self.timeout_ms,
            allow_credentials=descriptors,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        serialized = json.loads(options_to_json(options))
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM webauthn_authentication_challenges "
                "WHERE expires_at<=? OR consumed_at IS NOT NULL",
                (int(now),),
            )
            db.execute(
                "INSERT INTO webauthn_authentication_challenges"
                "(challenge,user_id,expires_at) VALUES(?,?,?)",
                (serialized["challenge"], user, int(now) + ttl_seconds),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return serialized

    def consume_enrollment_bootstrap_token(
        self,
        *,
        user_id: str,
        configured_token: str,
        supplied_token: str,
        now: int,
    ) -> bool:
        """Consume one out-of-band first-device token once per operator identity."""
        user = self._user_id(user_id)
        if (
            not isinstance(configured_token, str)
            or len(configured_token) < 32
            or not isinstance(supplied_token, str)
            or len(supplied_token) < 32
        ):
            return False
        configured_hash = hashlib.sha256(configured_token.encode("utf-8")).hexdigest()
        supplied_hash = hashlib.sha256(supplied_token.encode("utf-8")).hexdigest()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM webauthn_credentials WHERE user_id=? LIMIT 1",
                (user,),
            ).fetchone():
                db.commit()
                return False
            db.execute(
                "INSERT OR IGNORE INTO webauthn_enrollment_bootstrap"
                "(user_id,token_hash,consumed_at) VALUES(?,?,NULL)",
                (user, configured_hash),
            )
            row = db.execute(
                "SELECT token_hash,consumed_at FROM webauthn_enrollment_bootstrap "
                "WHERE user_id=?",
                (user,),
            ).fetchone()
            if (
                row is None
                or row[1] is not None
                or not hmac.compare_digest(row[0], configured_hash)
                or not hmac.compare_digest(row[0], supplied_hash)
            ):
                db.commit()
                return False
            changed = db.execute(
                "UPDATE webauthn_enrollment_bootstrap SET consumed_at=? "
                "WHERE user_id=? AND consumed_at IS NULL",
                (int(now), user),
            ).rowcount
            if changed != 1:
                db.rollback()
                return False
            db.execute(
                "INSERT INTO webauthn_credential_audit"
                "(event,user_id,credential_id,recorded_at) "
                "VALUES('BOOTSTRAP_TOKEN_CONSUMED',?,?,?)",
                (user, "first-device-bootstrap", int(now)),
            )
            db.commit()
            return True
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def reset_enrollment_bootstrap_token(
        self, *, user_id: str, new_token: str, now: int
    ) -> None:
        """Offline-admin recovery after every credential for this user was revoked."""
        user = self._user_id(user_id)
        if not isinstance(new_token, str) or len(new_token) < 32:
            raise ValueError("new bootstrap token must contain at least 256 bits")
        if self.credential_count(user_id=user) != 0:
            raise WebAuthnCredentialError(
                "bootstrap reset is allowed only after all user credentials are revoked"
            )
        token_hash = hashlib.sha256(new_token.encode("utf-8")).hexdigest()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM webauthn_credentials WHERE user_id=? LIMIT 1",
                (user,),
            ).fetchone():
                raise WebAuthnCredentialError(
                    "a credential was registered during bootstrap reset"
                )
            db.execute(
                "INSERT INTO webauthn_enrollment_bootstrap"
                "(user_id,token_hash,consumed_at) VALUES(?,?,NULL) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "token_hash=excluded.token_hash,consumed_at=NULL",
                (user, token_hash),
            )
            db.execute(
                "INSERT INTO webauthn_credential_audit"
                "(event,user_id,credential_id,recorded_at) "
                "VALUES('BOOTSTRAP_TOKEN_RESET',?,?,?)",
                (user, "first-device-bootstrap", int(now)),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_credentials(self, *, user_id: str) -> list[dict[str, Any]]:
        """Return non-secret authenticator metadata for one authenticated subject."""
        user = self._user_id(user_id)
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT credential_id,transports_json,device_type,backed_up,"
                "created_at,last_used_at FROM webauthn_credentials "
                "WHERE user_id=? ORDER BY created_at,credential_id",
                (user,),
            ).fetchall()
        finally:
            db.close()
        return [
            {
                "credential_id": row[0],
                "transports": json.loads(row[1]),
                "device_type": row[2],
                "backed_up": bool(row[3]),
                "created_at": int(row[4]),
                "last_used_at": int(row[5]) if row[5] is not None else None,
            }
            for row in rows
        ]

    def revoke_credential(self, *, user_id: str, credential_id: str) -> bool:
        """Offline-admin primitive; never exposed as a browser mutation route."""
        user = self._user_id(user_id)
        if not isinstance(credential_id, str) or not 16 <= len(credential_id) <= 1024:
            raise ValueError("valid credential identifier required")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT 1 FROM webauthn_credentials WHERE user_id=? AND credential_id=?",
                (user, credential_id),
            ).fetchone()
            if existing is None:
                db.commit()
                return False
            db.execute(
                "INSERT INTO webauthn_credential_audit"
                "(event,user_id,credential_id,recorded_at) "
                "VALUES('CREDENTIAL_REVOKED',?,?,?)",
                (user, credential_id, int(time.time())),
            )
            changed = db.execute(
                "DELETE FROM webauthn_credentials WHERE user_id=? AND credential_id=?",
                (user, credential_id),
            ).rowcount
            if changed != 1:
                raise WebAuthnCredentialError("credential revocation raced")
            db.commit()
            return True
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

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
            consumed = db.execute(
                "UPDATE webauthn_registration_challenges SET consumed_at=? "
                "WHERE challenge=? AND consumed_at IS NULL",
                (int(now), challenge),
            ).rowcount
            if consumed != 1:
                raise WebAuthnCredentialError("registration challenge replay")
            db.execute(
                "INSERT INTO webauthn_credential_audit"
                "(event,user_id,credential_id,recorded_at) "
                "VALUES('CREDENTIAL_REGISTERED',?,?,?)",
                (user, credential_key, int(now)),
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
        now: int | None = None,
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
        current_time = int(time.time()) if now is None else now
        if type(current_time) is not int or current_time < 0:
            raise WebAuthnCredentialError("invalid assertion verification time")
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
                challenge_row = db.execute(
                    "SELECT user_id,expires_at,consumed_at "
                    "FROM webauthn_authentication_challenges WHERE challenge=?",
                    (challenge,),
                ).fetchone()
                if (
                    challenge_row is None
                    or challenge_row[1] <= current_time
                    or challenge_row[2] is not None
                ):
                    raise WebAuthnCredentialError(
                        "authentication challenge missing, expired or already consumed"
                    )
                row = db.execute(
                    "SELECT user_id,public_key,sign_count FROM webauthn_credentials "
                    "WHERE credential_id=?",
                    (credential_id,),
                ).fetchone()
                if row is None:
                    raise WebAuthnCredentialError("unknown WebAuthn credential")
                user_id, public_key, current_count = row
                if user_id != challenge_row[0]:
                    raise WebAuthnCredentialError(
                        "authentication challenge user does not own credential"
                    )
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
                if type(verified.new_sign_count) is not int:
                    raise WebAuthnCredentialError(
                        "authenticator signature counter is invalid"
                    )
                new_sign_count = verified.new_sign_count
                if new_sign_count < 0:
                    raise WebAuthnCredentialError(
                        "authenticator signature counter is invalid"
                    )
                if int(current_count) > 0 and new_sign_count <= int(current_count):
                    raise WebAuthnCredentialError(
                        "authenticator signature counter rollback or clone detected"
                    )
                changed = db.execute(
                    "UPDATE webauthn_credentials SET sign_count=?,last_used_at=? "
                    "WHERE credential_id=? AND sign_count=?",
                    (
                        new_sign_count,
                        current_time,
                        credential_id,
                        int(current_count),
                    ),
                ).rowcount
                if changed != 1:
                    raise WebAuthnCredentialError(
                        "credential counter changed concurrently"
                    )
                consumed = db.execute(
                    "UPDATE webauthn_authentication_challenges SET consumed_at=? "
                    "WHERE challenge=? AND consumed_at IS NULL",
                    (current_time, challenge),
                ).rowcount
                if consumed != 1:
                    raise WebAuthnCredentialError("authentication challenge replay")
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
                    "new_sign_count": new_sign_count,
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
