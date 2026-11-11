import sys

import pytest

from aitos.native.windows_vault import VaultUnavailable, WindowsVault


class API:
    def __init__(self):
        self.values = {}

    def write(self, target, data):
        self.values[target] = data

    def read(self, target):
        return self.values.get(target)

    def delete(self, target):
        return self.values.pop(target, None) is not None


def test_no_plaintext_fallback_off_windows():
    if sys.platform != "win32":
        with pytest.raises(VaultUnavailable):
            WindowsVault()


def test_fake_transport_namespace_unicode_replace_delete():
    api = API()
    vault = WindowsVault(api=api)
    assert vault.get("test/one") is None
    vault.put("test/one", "synthetic-λ-secret")
    assert api.values["AITOS/v1/test/one"] == "synthetic-λ-secret".encode("utf-16-le")
    assert vault.get("test/one") == "synthetic-λ-secret"
    vault.put("test/one", "replacement")
    assert vault.get("test/one") == "replacement"
    assert vault.delete("test/one")
    assert not vault.delete("test/one")


@pytest.mark.parametrize("slot", ["", "../secret", "bad space", "x" * 121, True])
def test_bad_slot_fails_before_native_access(slot):
    api = API()
    vault = WindowsVault(api=api)
    with pytest.raises(ValueError):
        vault.put(slot, "fake")
    assert not api.values


@pytest.mark.parametrize("secret", ["", "a\0b", "x" * 1281, None, True])
def test_bad_or_oversize_secret_never_writes(secret):
    api = API()
    vault = WindowsVault(api=api)
    with pytest.raises(ValueError):
        vault.put("test", secret)
    assert not api.values


def test_native_failure_propagates_without_secret_in_exception():
    class Broken(API):
        def write(self, *args):
            raise VaultUnavailable("Native credential write failed")

    with pytest.raises(VaultUnavailable) as failure:
        WindowsVault(api=Broken()).put("test", "do-not-log-this")
    assert "do-not-log-this" not in str(failure.value)
