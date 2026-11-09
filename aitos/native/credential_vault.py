"""Fail-closed OS credential vault selection."""

from __future__ import annotations

import re
import sys


class VaultUnavailable(RuntimeError):
    """The operating-system credential service is unavailable or unsupported."""


class KeyringVault:
    def __init__(self, *, keyring_module=None, platform=None):
        self._platform = platform or sys.platform
        if keyring_module is None:
            try:
                import keyring as keyring_module
            except ImportError:
                raise VaultUnavailable(
                    "OS credential service dependency unavailable"
                ) from None
        self._keyring = keyring_module
        try:
            backend = keyring_module.get_keyring()
            backend_name = f"{type(backend).__module__}.{type(backend).__name__}"
            priority = backend.priority
        except Exception:
            raise VaultUnavailable("OS credential service unavailable") from None
        if not isinstance(priority, (int, float)) or priority <= 0:
            raise VaultUnavailable("No secure OS credential backend is available")
        if self._platform.startswith("linux") and "SecretService" not in backend_name:
            raise VaultUnavailable("Linux Secret Service backend is required")
        if self._platform == "darwin" and "Keyring" not in backend_name:
            raise VaultUnavailable("macOS Keychain backend is required")

    @staticmethod
    def _target(slot):
        if type(slot) is not str or not re.fullmatch(
            r"[a-zA-Z0-9_.:/-]{1,120}", slot
        ):
            raise ValueError("invalid vault slot")
        return "AITOS/v1/" + slot

    @staticmethod
    def _validate(secret):
        if type(secret) is not str or not secret or "\0" in secret:
            raise ValueError("nonempty secret required")
        if len(secret.encode("utf-8")) > 2560:
            raise ValueError("native secret size exceeded")

    def put(self, slot, secret):
        target = self._target(slot)
        self._validate(secret)
        try:
            self._keyring.set_password("AITOS", target, secret)
        except Exception:
            raise VaultUnavailable("OS credential write failed") from None

    def get(self, slot):
        try:
            return self._keyring.get_password("AITOS", self._target(slot))
        except Exception:
            raise VaultUnavailable("OS credential read failed") from None

    def delete(self, slot):
        try:
            self._keyring.delete_password("AITOS", self._target(slot))
            return True
        except self._keyring.errors.PasswordDeleteError:
            return False
        except Exception:
            raise VaultUnavailable("OS credential deletion failed") from None


def get_credential_vault():
    """Select the OS-native vault; never silently downgrade to plaintext."""
    if sys.platform == "win32":
        from aitos.native.windows_vault import WindowsVault

        return WindowsVault()
    if sys.platform.startswith("linux") or sys.platform == "darwin":
        return KeyringVault()
    raise VaultUnavailable("unsupported operating system credential store")
