"""Durable credential-mode policy and fail-closed provider routing.

This module stores only non-secret routing metadata. External provider adapters
are injected by the trusted signer process; no provider token or exchange secret
is persisted in this database.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Protocol

from aitos.native.credential_vault import CredentialStore, VaultUnavailable


class CredentialProfileError(RuntimeError):
    """Credential policy is missing, invalid, or its provider is unavailable."""


@dataclass(frozen=True)
class CredentialProfile:
    mode: str
    provider_id: str | None = None
    secret_ref: str | None = None


class ExternalVaultBackend(Protocol):
    """Provider adapter contract; implementations own their authentication."""

    def get_secret(self, reference: str) -> str | None: ...

    def put_secret(self, reference: str, secret: str) -> None: ...

    def delete_secret(self, reference: str) -> bool: ...


class CredentialProfileStore:
    """SQLite-backed global default and per-account overrides (metadata only)."""

    MODES = frozenset({"native", "external"})

    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise CredentialProfileError("credential_profile_path_must_not_be_symlink")
        if os.name == "posix":
            try:
                self.path.parent.chmod(0o700)
            except OSError:
                raise CredentialProfileError(
                    "credential_profile_directory_permissions_unavailable"
                ) from None
        self._db = sqlite3.connect(self.path, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS credential_default (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                mode TEXT NOT NULL CHECK(mode IN ('native', 'external')),
                provider_id TEXT,
                secret_ref TEXT
            )"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS credential_account_profiles (
                account_alias TEXT PRIMARY KEY,
                mode TEXT NOT NULL CHECK(mode IN ('native', 'external')),
                provider_id TEXT,
                secret_ref TEXT
            )"""
        )
        if os.name == "posix":
            try:
                self.path.chmod(0o600)
            except OSError:
                self.close()
                raise CredentialProfileError(
                    "credential_profile_permissions_unavailable"
                ) from None

    @staticmethod
    def _account(alias: str) -> str:
        if type(alias) is not str or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", alias
        ):
            raise ValueError("invalid_account_alias")
        return alias

    @staticmethod
    def _profile(
        mode: str, provider_id: str | None, secret_ref: str | None
    ) -> CredentialProfile:
        if mode not in CredentialProfileStore.MODES:
            raise ValueError("invalid_credential_mode")
        if mode == "native":
            if provider_id is not None or secret_ref is not None:
                raise ValueError("native_profile_must_not_have_external_reference")
        else:
            if type(provider_id) is not str or not re.fullmatch(
                r"[a-z][a-z0-9_-]{1,31}", provider_id
            ):
                raise ValueError("external_provider_id_required")
            if (
                type(secret_ref) is not str
                or not secret_ref
                or len(secret_ref) > 512
                or "\x00" in secret_ref
            ):
                raise ValueError("external_secret_reference_required")
        return CredentialProfile(mode, provider_id, secret_ref)

    def set_default(
        self,
        mode: str,
        *,
        provider_id: str | None = None,
        secret_ref: str | None = None,
    ) -> None:
        profile = self._profile(mode, provider_id, secret_ref)
        self._db.execute(
            """INSERT INTO credential_default(
                   singleton, mode, provider_id, secret_ref
               ) VALUES(1, ?, ?, ?)
               ON CONFLICT(singleton) DO UPDATE SET
                   mode=excluded.mode,
                   provider_id=excluded.provider_id,
                   secret_ref=excluded.secret_ref""",
            (profile.mode, profile.provider_id, profile.secret_ref),
        )

    def set_account(
        self,
        account_alias: str,
        mode: str,
        *,
        provider_id: str | None = None,
        secret_ref: str | None = None,
    ) -> None:
        alias = self._account(account_alias)
        profile = self._profile(mode, provider_id, secret_ref)
        self._db.execute(
            """INSERT INTO credential_account_profiles(
                   account_alias, mode, provider_id, secret_ref
               ) VALUES(?, ?, ?, ?)
               ON CONFLICT(account_alias) DO UPDATE SET
                   mode=excluded.mode,
                   provider_id=excluded.provider_id,
                   secret_ref=excluded.secret_ref""",
            (alias, profile.mode, profile.provider_id, profile.secret_ref),
        )

    def resolve(self, account_alias: str) -> CredentialProfile:
        alias = self._account(account_alias)
        row = self._db.execute(
            "SELECT mode, provider_id, secret_ref "
            "FROM credential_account_profiles WHERE account_alias=?",
            (alias,),
        ).fetchone()
        if row is None:
            row = self._db.execute(
                "SELECT mode, provider_id, secret_ref "
                "FROM credential_default WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise CredentialProfileError("credential_profile_not_configured")
        return self._profile(*row)

    def close(self) -> None:
        self._db.close()


class LazyNativeCredentialStore:
    """Initialize the OS vault only if a selected profile actually uses it."""

    def __init__(self, factory):
        self._factory = factory
        self._store: CredentialStore | None = None

    def _get_store(self) -> CredentialStore:
        if self._store is None:
            try:
                self._store = self._factory()
            except Exception:
                raise VaultUnavailable(
                    "native credential provider unavailable"
                ) from None
        return self._store

    def put(self, slot: str, secret: str) -> None:
        self._get_store().put(slot, secret)

    def get(self, slot: str) -> str | None:
        return self._get_store().get(slot)

    def delete(self, slot: str) -> bool:
        return self._get_store().delete(slot)


class CredentialProviderRouter:
    """Routes signer slots by account policy; never silently falls back."""

    def __init__(
        self,
        *,
        profiles: CredentialProfileStore,
        native_store: CredentialStore,
        external_backends: dict[str, ExternalVaultBackend] | None = None,
    ):
        self._profiles = profiles
        self._native = native_store
        self._external = dict(external_backends or {})

    @staticmethod
    def _slot_parts(slot: str) -> tuple[str, str]:
        match = re.fullmatch(
            r"signer/([A-Za-z0-9][A-Za-z0-9._:-]{0,63})/(key-id|api-secret)",
            slot or "",
        )
        if not match:
            raise ValueError("invalid_signer_credential_slot")
        return match.group(1), match.group(2)

    def _route(self, slot: str):
        account, part = self._slot_parts(slot)
        profile = self._profiles.resolve(account)
        if profile.mode == "native":
            return profile.mode, self._native, slot
        backend = self._external.get(profile.provider_id or "")
        if backend is None:
            raise VaultUnavailable(
                "configured external credential provider unavailable"
            )
        return (
            profile.mode,
            backend,
            f"{profile.secret_ref.rstrip('/')}/{part}",
        )

    def put(self, slot: str, secret: str) -> None:
        mode, backend, reference = self._route(slot)
        if (
            type(secret) is not str
            or not secret
            or "\x00" in secret
            or len(secret.encode("utf-8")) > 2560
        ):
            raise ValueError("invalid_credential_value")
        if mode == "native":
            backend.put(reference, secret)
        else:
            backend.put_secret(reference, secret)

    def get(self, slot: str) -> str | None:
        mode, backend, reference = self._route(slot)
        if mode == "native":
            value = backend.get(reference)
        else:
            value = backend.get_secret(reference)
        if value is not None and (
            type(value) is not str
            or not value
            or "\x00" in value
            or len(value.encode("utf-8")) > 2560
        ):
            raise VaultUnavailable("external credential value invalid")
        return value

    def delete(self, slot: str) -> bool:
        mode, backend, reference = self._route(slot)
        if mode == "native":
            return backend.delete(reference)
        return backend.delete_secret(reference)


def discover_external_vault_backends() -> dict[str, ExternalVaultBackend]:
    """Load explicitly installed signer-host adapters via Python entry points.

    Entry-point group: aitos.credential_vaults. Each entry point name is the
    provider ID and must load a zero-argument factory returning a backend.
    """
    discovered: dict[str, ExternalVaultBackend] = {}
    # First-party CLI adapters are available without a separate Python package.
    # Their provider CLIs must already be installed and unlocked on the signer host.
    from aitos.native.cli_credential_vaults import (
        bitwarden_vault_factory,
        onepassword_vault_factory,
    )

    builtin_factories = {
        "bitwarden": bitwarden_vault_factory,
        "onepassword": onepassword_vault_factory,
    }
    try:
        for provider_id, factory in builtin_factories.items():
            discovered[provider_id] = factory()
        points = entry_points(group="aitos.credential_vaults")
        for point in points:
            provider_id = point.name
            if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", provider_id):
                raise CredentialProfileError("invalid_external_provider_registration")
            if provider_id in discovered:
                raise CredentialProfileError("duplicate_external_provider_registration")
            factory = point.load()
            backend = factory()
            if not all(
                callable(getattr(backend, method, None))
                for method in ("get_secret", "put_secret", "delete_secret")
            ):
                raise CredentialProfileError("external_provider_contract_invalid")
            discovered[provider_id] = backend
    except CredentialProfileError:
        raise
    except Exception:
        # Do not leak plugin import errors, auth tokens, or provider diagnostics.
        raise CredentialProfileError(
            "external_provider_initialization_failed"
        ) from None
    return discovered
