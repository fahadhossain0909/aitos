"""Local signer credential slots backed only by an injected OS credential store.

Instantiate this helper only inside the user-controlled signer boundary. Never
run it on a provider-managed VPS/RDP when the provider-admin threat is in scope.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from aitos.native.credential_vault import CredentialStore


class SignerCredentialError(RuntimeError):
    """Signer credential material is missing or unavailable."""


@dataclass(frozen=True)
class SignerCredentialReference:
    account_alias: str
    key_id: str


class SignerCredentialSlots:
    def __init__(self, store: CredentialStore) -> None:
        if not all(
            callable(getattr(store, name, None)) for name in ("put", "get", "delete")
        ):
            raise TypeError("native_credential_store_required")
        self._store = store

    @staticmethod
    def _alias(account_alias: str) -> str:
        if type(account_alias) is not str or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", account_alias
        ):
            raise ValueError("invalid_account_alias")
        return account_alias

    def save(
        self, account_alias: str, *, key_id: str, api_secret: str
    ) -> SignerCredentialReference:
        alias = self._alias(account_alias)
        if type(key_id) is not str or not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,128}", key_id
        ):
            raise ValueError("invalid_exchange_key_id")
        if type(api_secret) is not str or not api_secret or "\x00" in api_secret:
            raise ValueError("invalid_exchange_secret")
        self._store.put(f"signer/{alias}/key-id", key_id)
        try:
            self._store.put(f"signer/{alias}/api-secret", api_secret)
        except Exception:
            # Do not leave a partial credential pair that could be mistaken as ready.
            try:
                self._store.delete(f"signer/{alias}/key-id")
            except Exception:
                pass
            raise SignerCredentialError("signer_credential_save_failed") from None
        return SignerCredentialReference(account_alias=alias, key_id=key_id)

    def resolve(self, account_alias: str) -> tuple[str, str]:
        alias = self._alias(account_alias)
        try:
            key_id = self._store.get(f"signer/{alias}/key-id")
            secret = self._store.get(f"signer/{alias}/api-secret")
        except Exception:
            raise SignerCredentialError("signer_credential_read_failed") from None
        if not key_id or not secret:
            raise SignerCredentialError("signer_credential_pair_incomplete")
        return key_id, secret

    def delete(self, account_alias: str) -> bool:
        alias = self._alias(account_alias)
        try:
            secret_deleted = self._store.delete(f"signer/{alias}/api-secret")
            key_deleted = self._store.delete(f"signer/{alias}/key-id")
        except Exception:
            raise SignerCredentialError("signer_credential_delete_failed") from None
        return bool(secret_deleted or key_deleted)
