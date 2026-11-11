"""Pinned OS credential stores; never use a global/file/fallback keyring."""

from __future__ import annotations

import importlib
import math
import re
import sys
from typing import Protocol


class VaultUnavailable(RuntimeError):
    """The operating-system credential service is unavailable or unsupported."""


class CredentialStore(Protocol):
    def put(self, slot: str, secret: str) -> None: ...
    def get(self, slot: str) -> str | None: ...
    def delete(self, slot: str) -> bool: ...


class KeyringVault:
    def __init__(self, *, keyring_module=None, platform=None):
        platform = platform or sys.platform
        module = (
            "keyring.backends.SecretService"
            if platform.startswith("linux")
            else "keyring.backends.macOS" if platform == "darwin" else None
        )
        if module is None:
            raise VaultUnavailable("unsupported OS credential service")
        try:
            # Do NOT delegate operations to keyring.set_password: another caller
            # can replace its global backend after validation. Pin one instance.
            backend = (
                importlib.import_module(module).Keyring()
                if keyring_module is None
                else keyring_module.get_keyring()
            )
            identity = (type(backend).__module__, type(backend).__name__)
            priority = backend.priority
            if identity != (module, "Keyring") or (
                isinstance(priority, bool)
                or not isinstance(priority, (int, float))
                or not math.isfinite(priority)
                or priority <= 0
            ):
                raise ValueError()
            self._backend = backend
        except Exception:
            raise VaultUnavailable(
                "Required native credential backend unavailable"
            ) from None

    @staticmethod
    def _target(slot):
        if (
            type(slot) is not str
            or not re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,120}", slot)
            or slot.startswith("/")
            or any(part in {".", ".."} for part in slot.split("/"))
        ):
            raise ValueError("invalid vault slot")
        return "AITOS/v1/" + slot

    @staticmethod
    def _validate(secret):
        if type(secret) is not str or not secret or "\0" in secret:
            raise ValueError("nonempty secret required")
        try:
            if len(secret.encode("utf-8")) > 2560:
                raise ValueError("native secret size exceeded")
        except UnicodeError:
            raise ValueError("invalid secret encoding") from None

    def put(self, slot, secret):
        target = self._target(slot)
        self._validate(secret)
        try:
            self._backend.set_password("AITOS", target, secret)
        except Exception:
            raise VaultUnavailable("OS credential write failed") from None

    def get(self, slot):
        target = self._target(slot)
        try:
            value = self._backend.get_password("AITOS", target)
            if value is not None:
                self._validate(value)
            return value
        except Exception:
            raise VaultUnavailable("OS credential read failed") from None

    def delete(self, slot):
        target = self._target(slot)
        if self.get(slot) is None:
            return False
        try:
            self._backend.delete_password("AITOS", target)
            return True
        except Exception:
            # Access-denied/deletion errors are NOT evidence of an absent item.
            raise VaultUnavailable("OS credential deletion failed") from None


def get_credential_vault() -> CredentialStore:
    if sys.platform == "win32":
        from aitos.native.windows_vault import WindowsVault

        return WindowsVault()
    return KeyringVault()
