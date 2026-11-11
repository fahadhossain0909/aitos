import pytest

from aitos.security.signer_rpc_protocol import SignerProtocolRejected
from aitos.security.signer_service import (
    SQLiteSignerRateLimiter,
    SQLiteSignerRegistry,
)


IDENTITY = "spiffe://aitos/signer/client-01"
NOW = 1_800_000_000_000


def enroll(
    registry, serial="A1B2", valid_from_ms=NOW - 1000, valid_until_ms=NOW + 100_000
):
    return registry.enroll(
        identity=IDENTITY,
        account_alias="paper-primary",
        allowed_symbols={"BTCUSDT", "ETHUSDT"},
        certificate_serial=serial,
        valid_from_ms=valid_from_ms,
        valid_until_ms=valid_until_ms,
        now_ms=NOW,
    )


def test_enrollment_survives_restart_and_rotation_overlaps(tmp_path):
    path = tmp_path / "registry.sqlite3"
    first = SQLiteSignerRegistry(path)
    grant = enroll(first)
    assert grant.allowed_serials == frozenset({"A1B2"})
    rotated = enroll(first, "C3D4", valid_from_ms=NOW + 10, valid_until_ms=NOW + 20_000)
    assert rotated.allowed_serials == frozenset({"A1B2", "C3D4"})
    assert rotated.serial_validity["A1B2"] == (NOW - 1000, NOW + 100_000)
    assert rotated.serial_validity["C3D4"] == (NOW + 10, NOW + 20_000)
    first.close()

    reopened = SQLiteSignerRegistry(path)
    restored = reopened.get_registry()._grants[IDENTITY]
    assert restored.allowed_serials == frozenset({"A1B2", "C3D4"})
    assert restored.allowed_symbols == frozenset({"BTCUSDT", "ETHUSDT"})
    assert restored.serial_validity["C3D4"] == (NOW + 10, NOW + 20_000)
    registry = reopened.get_registry()
    registry.authorize(
        {"subjectAltName": [("URI", IDENTITY)], "serialNumber": "A1B2"},
        now_ms=NOW + 50_000,
    )
    with pytest.raises(
        SignerProtocolRejected, match="client_certificate_not_authorized"
    ):
        registry.authorize(
            {"subjectAltName": [("URI", IDENTITY)], "serialNumber": "C3D4"},
            now_ms=NOW + 50_000,
        )
    reopened.close()


def test_revocation_is_durable_and_revoked_serial_cannot_be_reenrolled(tmp_path):
    path = tmp_path / "registry.sqlite3"
    registry = SQLiteSignerRegistry(path)
    enroll(registry)
    registry.revoke(IDENTITY, "A1B2", now_ms=NOW + 1)
    registry.close()

    reopened = SQLiteSignerRegistry(path)
    grant = reopened.get_registry()._grants[IDENTITY]
    assert "A1B2" in grant.revoked_serials
    with pytest.raises(ValueError, match="revoked_certificate_serial"):
        enroll(reopened)
    reopened.close()


def test_disable_persists_and_invalid_enrollment_is_rejected(tmp_path):
    registry = SQLiteSignerRegistry(tmp_path / "registry.sqlite3")
    enroll(registry)
    registry.disable(IDENTITY, now_ms=NOW + 1)
    enroll(registry, "C3D4")
    assert registry.get_registry()._grants[IDENTITY].enabled is False
    registry.enable(IDENTITY, now_ms=NOW + 2)
    assert registry.get_registry()._grants[IDENTITY].enabled is True
    with pytest.raises(ValueError, match="invalid_signer_symbol_grant"):
        registry.enroll(
            identity=IDENTITY,
            account_alias="paper-primary",
            allowed_symbols={"bad symbol"},
            certificate_serial="F00D",
            valid_from_ms=NOW,
            valid_until_ms=NOW + 100,
            now_ms=NOW,
        )
    registry.close()


def test_rate_limiter_is_atomic_and_restart_safe(tmp_path):
    path = tmp_path / "rate.sqlite3"
    first = SQLiteSignerRateLimiter(path, limit=2, window_ms=1000)
    first.consume(IDENTITY, "paper-primary", now_ms=NOW)
    first.consume(IDENTITY, "paper-primary", now_ms=NOW + 1)
    with pytest.raises(SignerProtocolRejected, match="signer_rate_limit_exceeded"):
        first.consume(IDENTITY, "paper-primary", now_ms=NOW + 2)
    first.close()

    reopened = SQLiteSignerRateLimiter(path, limit=2, window_ms=1000)
    with pytest.raises(SignerProtocolRejected, match="signer_rate_limit_exceeded"):
        reopened.consume(IDENTITY, "paper-primary", now_ms=NOW + 3)
    reopened.consume(IDENTITY, "paper-primary", now_ms=NOW + 1000)
    reopened.close()


def test_rate_limit_scopes_are_isolated(tmp_path):
    with SQLiteSignerRateLimiter(
        tmp_path / "rate.sqlite3", limit=1, window_ms=1000
    ) as limiter:
        limiter.consume(IDENTITY, "paper-primary", now_ms=NOW)
        limiter.consume(IDENTITY, "paper-secondary", now_ms=NOW)
        limiter.consume("spiffe://aitos/signer/other", "paper-primary", now_ms=NOW)


def test_registry_requires_private_durable_storage(tmp_path):
    with pytest.raises(ValueError, match="durable_signer_registry_required"):
        SQLiteSignerRegistry(":memory:")
    with pytest.raises(ValueError, match="invalid_signer_rate_window"):
        SQLiteSignerRateLimiter(tmp_path / "rate.sqlite3", limit=1, window_ms=1)


def test_reduce_only_rpc_has_separate_bounded_rate_bucket(tmp_path):
    limiter = SQLiteSignerRateLimiter(
        tmp_path / "rate.sqlite3",
        limit=1,
        window_ms=1000,
        reduce_only_limit=2,
    )
    limiter.consume(IDENTITY, "paper-primary", now_ms=NOW)
    with pytest.raises(SignerProtocolRejected, match="signer_rate_limit_exceeded"):
        limiter.consume(IDENTITY, "paper-primary", now_ms=NOW + 1)
    limiter.consume(IDENTITY, "paper-primary", now_ms=NOW + 2, is_reduce_only=True)
    limiter.consume(IDENTITY, "paper-primary", now_ms=NOW + 3, is_reduce_only=True)
    with pytest.raises(SignerProtocolRejected, match="signer_rate_limit_exceeded"):
        limiter.consume(IDENTITY, "paper-primary", now_ms=NOW + 4, is_reduce_only=True)
    limiter.close()
