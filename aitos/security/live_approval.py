"""Fail-closed live approval control-plane primitives.

A WebAuthn Relying Party adapter MUST validate clientDataJSON, challenge,
origin, RP ID hash, authenticator flags, credential ownership, signature,
sign-counter policy and user verification. This module deliberately accepts
only a verifier callback and never treats a typed phrase or self-asserted MFA
flag as authentication. Tokens are short-lived, signed and single-use.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class ApprovalDenied(PermissionError):
    pass


@dataclass(frozen=True)
class ApprovalChallenge:
    challenge_id: str
    challenge: str
    user_id: str
    action_digest: str
    expires_at: int


@dataclass(frozen=True)
class SignedApproval:
    token_id: str
    user_id: str
    action_digest: str
    issued_at: int
    expires_at: int
    signature: str


class LiveApprovalControlPlane:
    def __init__(
        self,
        path: str | Path,
        *,
        signing_key: bytes,
        allowed_origins: set[str],
        rp_id: str,
        ttl_seconds: int = 90,
        role_authorizer: Callable[[str, str], bool] | None = None,
    ):
        if len(signing_key) < 32:
            raise ValueError("approval signing key must contain at least 256 bits")
        if not allowed_origins or not rp_id or not 1 <= ttl_seconds <= 300:
            raise ValueError("explicit WebAuthn origin, RP ID and short TTL required")
        self.path = Path(path).expanduser()
        if self.path.is_symlink():
            raise ValueError("approval database symlinks are forbidden")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.is_symlink():
            raise ValueError("approval database directory symlinks are forbidden")
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
        self.key = bytes(signing_key)
        self.origins = frozenset(allowed_origins)
        self.rp_id = rp_id
        self.ttl = ttl_seconds
        if not callable(role_authorizer):
            raise ValueError("server-side RBAC role_authorizer is required")
        self.role_authorizer = role_authorizer
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
            CREATE TABLE IF NOT EXISTS live_approval_challenges(
              challenge_id TEXT PRIMARY KEY, challenge TEXT NOT NULL,
              user_id TEXT NOT NULL, action_digest TEXT NOT NULL,
              expires_at INTEGER NOT NULL, consumed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS live_approval_tokens(
              token_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
              action_digest TEXT NOT NULL, issued_at INTEGER NOT NULL,
              expires_at INTEGER NOT NULL, signature TEXT NOT NULL,
              consumed_at INTEGER);
            CREATE TABLE IF NOT EXISTS live_approval_audit(
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL,
              user_id TEXT NOT NULL, action_digest TEXT NOT NULL,
              token_id TEXT, recorded_at INTEGER NOT NULL);
            CREATE TRIGGER IF NOT EXISTS live_approval_audit_no_update
              BEFORE UPDATE ON live_approval_audit BEGIN
                SELECT RAISE(ABORT,'approval audit is append-only');
              END;
            CREATE TRIGGER IF NOT EXISTS live_approval_audit_no_delete
              BEFORE DELETE ON live_approval_audit BEGIN
                SELECT RAISE(ABORT,'approval audit is append-only');
              END;
            """
            )
        if self.path.exists() and os.name != "nt":
            os.chmod(self.path, 0o600)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA busy_timeout=5000")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _digest(action: dict) -> str:
        if type(action) is not dict or not action:
            raise ValueError("canonical live action required")
        raw = json.dumps(
            action,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        if len(raw) > 65536:
            raise ValueError("approval action too large")
        return hashlib.sha256(raw).hexdigest()

    def issue_challenge(
        self,
        *,
        user_id: str,
        action: dict,
        required_role: str = "live_trader",
        now: int | None = None,
        webauthn_challenge: str | None = None,
    ):
        if (
            not isinstance(user_id, str)
            or not user_id
            or not self.role_authorizer(user_id, required_role)
        ):
            raise ApprovalDenied("server-side RBAC denied live approval request")
        now = int(time.time()) if now is None else int(now)
        challenge_id = secrets.token_urlsafe(24)
        challenge = webauthn_challenge or secrets.token_urlsafe(32)
        if not isinstance(challenge, str) or not 16 <= len(challenge) <= 256:
            raise ValueError("bounded server-issued WebAuthn challenge required")
        digest = self._digest(action)
        expiry = now + self.ttl
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO live_approval_challenges VALUES(?,?,?,?,?,0)",
                (challenge_id, challenge, user_id, digest, expiry),
            )
            db.execute(
                "INSERT INTO live_approval_audit(event,user_id,action_digest,recorded_at) "
                "VALUES('CHALLENGE_ISSUED',?,?,?)",
                (user_id, digest, now),
            )
        return ApprovalChallenge(challenge_id, challenge, user_id, digest, expiry)

    def load_challenge(self, challenge_id: str, *, user_id: str):
        """Load an unconsumed challenge only for its authenticated owner."""
        if (
            not isinstance(challenge_id, str)
            or not 16 <= len(challenge_id) <= 128
            or not isinstance(user_id, str)
            or not user_id
        ):
            raise ApprovalDenied("invalid approval challenge identity")
        with self._connection() as db:
            row = db.execute(
                "SELECT challenge,user_id,action_digest,expires_at,consumed "
                "FROM live_approval_challenges WHERE challenge_id=?",
                (challenge_id,),
            ).fetchone()
        if (
            row is None
            or row[1] != user_id
            or row[4]
            or int(row[3]) <= int(time.time())
        ):
            raise ApprovalDenied("approval challenge is unavailable")
        return ApprovalChallenge(
            challenge_id, row[0], row[1], row[2], int(row[3])
        )

    def approve(
        self,
        challenge: ApprovalChallenge,
        *,
        assertion: bytes,
        verifier: Callable[..., dict],
        now: int | None = None,
    ) -> SignedApproval:
        now = int(time.time()) if now is None else int(now)
        if not assertion or not callable(verifier):
            raise ApprovalDenied("verified WebAuthn assertion is required")
        # The RP adapter returns claims only after cryptographic and origin/RP checks.
        claims = verifier(
            assertion=assertion,
            challenge=challenge.challenge,
            expected_origin=self.origins,
            expected_rp_id=self.rp_id,
            require_user_verification=True,
        )
        if (
            not isinstance(claims, dict)
            or claims.get("verified") is not True
            or claims.get("user_id") != challenge.user_id
            or claims.get("user_verified") is not True
            or claims.get("origin") not in self.origins
            or claims.get("rp_id") != self.rp_id
        ):
            raise ApprovalDenied("WebAuthn assertion verification failed")
        token_id = secrets.token_urlsafe(24)
        expiry = min(challenge.expires_at, now + self.ttl)
        if now >= expiry:
            raise ApprovalDenied("approval challenge expired")
        message = self._message(
            token_id, challenge.user_id, challenge.action_digest, now, expiry
        )
        signature = hmac.new(self.key, message, hashlib.sha256).hexdigest()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT challenge,user_id,action_digest,expires_at,consumed "
                "FROM live_approval_challenges WHERE challenge_id=?",
                (challenge.challenge_id,),
            ).fetchone()
            if (
                row is None
                or row[4]
                or row[3] <= now
                or row[:3]
                != (challenge.challenge, challenge.user_id, challenge.action_digest)
            ):
                raise ApprovalDenied("challenge missing, expired, consumed or altered")
            db.execute(
                "UPDATE live_approval_challenges SET consumed=1 WHERE challenge_id=?",
                (challenge.challenge_id,),
            )
            db.execute(
                "INSERT INTO live_approval_tokens VALUES(?,?,?,?,?,?,NULL)",
                (
                    token_id,
                    challenge.user_id,
                    challenge.action_digest,
                    now,
                    expiry,
                    signature,
                ),
            )
            db.execute(
                "INSERT INTO live_approval_audit(event,user_id,action_digest,token_id,recorded_at) "
                "VALUES('APPROVAL_ISSUED',?,?,?,?)",
                (challenge.user_id, challenge.action_digest, token_id, now),
            )
        return SignedApproval(
            token_id, challenge.user_id, challenge.action_digest, now, expiry, signature
        )

    @staticmethod
    def _message(token_id, user_id, digest, issued, expires):
        return json.dumps(
            [token_id, user_id, digest, issued, expires], separators=(",", ":")
        ).encode()

    def get_unconsumed_token(self, action: dict) -> SignedApproval:
        """Trusted server-side provider for the exact approved action digest."""
        digest = self._digest(action)
        now = int(time.time())
        with self._connection() as db:
            row = db.execute(
                "SELECT token_id,user_id,action_digest,issued_at,expires_at,signature "
                "FROM live_approval_tokens WHERE action_digest=? "
                "AND consumed_at IS NULL AND expires_at>? "
                "ORDER BY issued_at,token_id LIMIT 1",
                (digest, now),
            ).fetchone()
        if row is None:
            raise ApprovalDenied("no unconsumed approval exists for this exact action")
        return SignedApproval(
            token_id=row[0],
            user_id=row[1],
            action_digest=row[2],
            issued_at=int(row[3]),
            expires_at=int(row[4]),
            signature=row[5],
        )

    def consume(
        self,
        token: SignedApproval,
        *,
        action: dict,
        required_role: str = "live_trader",
        now: int | None = None,
    ) -> str:
        now = int(time.time()) if now is None else int(now)
        if not self.role_authorizer(token.user_id, required_role):
            raise ApprovalDenied("server-side RBAC denied execution")
        digest = self._digest(action)
        expected = hmac.new(
            self.key,
            self._message(
                token.token_id,
                token.user_id,
                token.action_digest,
                token.issued_at,
                token.expires_at,
            ),
            hashlib.sha256,
        ).hexdigest()
        if (
            not hmac.compare_digest(expected, token.signature)
            or digest != token.action_digest
            or now < token.issued_at
            or now >= token.expires_at
        ):
            raise ApprovalDenied("invalid, mismatched or expired approval token")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT user_id,action_digest,issued_at,expires_at,signature,consumed_at "
                "FROM live_approval_tokens WHERE token_id=?",
                (token.token_id,),
            ).fetchone()
            if row is None or row != (
                token.user_id,
                digest,
                token.issued_at,
                token.expires_at,
                token.signature,
                None,
            ):
                raise ApprovalDenied(
                    "approval token already consumed or not issued here"
                )
            db.execute(
                "UPDATE live_approval_tokens SET consumed_at=? WHERE token_id=? AND consumed_at IS NULL",
                (now, token.token_id),
            )
            if db.total_changes < 1:
                raise ApprovalDenied("approval token replay")
            db.execute(
                "INSERT INTO live_approval_audit(event,user_id,action_digest,token_id,recorded_at) "
                "VALUES('APPROVAL_CONSUMED',?,?,?,?)",
                (token.user_id, digest, token.token_id, now),
            )
        return token.user_id
