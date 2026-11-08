"""Actual WinCred exercise on Windows only, synthetic unique credential slots."""

import sys
import uuid
import pytest
from aitos.native.windows_vault import WindowsVault


@pytest.mark.skipif(
    sys.platform != "win32", reason="requires Windows Credential Manager"
)
def test_native_current_user_credential_roundtrip():
    vault = WindowsVault()
    slot = "tests/" + uuid.uuid4().hex
    try:
        assert vault.get(slot) is None
        vault.put(slot, "synthetic-only-credential-λ")
        assert vault.get(slot) == "synthetic-only-credential-λ"
        vault.put(slot, "synthetic-replacement")
        assert vault.get(slot) == "synthetic-replacement"
        assert vault.delete(slot) is True
        assert vault.get(slot) is None
        assert vault.delete(slot) is False
    finally:
        vault.delete(slot)
