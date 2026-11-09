from types import SimpleNamespace

import pytest

from aitos.native.credential_vault import KeyringVault, VaultUnavailable


class Backend:
    priority = 1

    def __init__(self):
        self.values = {}

    def set_password(self, service, username, password):
        self.values[(service, username)] = password

    def get_password(self, service, username):
        return self.values.get((service, username))

    def delete_password(self, service, username):
        del self.values[(service, username)]


def fake_service(module="keyring.backends.SecretService", name="Keyring"):
    backend = type(name, (Backend,), {"__module__": module})()
    return SimpleNamespace(get_keyring=lambda: backend), backend


@pytest.mark.parametrize(
    "platform,module",
    [("linux", "keyring.backends.SecretService"), ("darwin", "keyring.backends.macOS")],
)
def test_native_backend_roundtrip(platform, module):
    fake, backend = fake_service(module)
    vault = KeyringVault(keyring_module=fake, platform=platform)
    vault.put("console/operator-password", "synthetic-secret")
    assert vault.get("console/operator-password") == "synthetic-secret"
    assert vault.delete("console/operator-password")
    assert not vault.delete("console/operator-password")
    assert not backend.values


@pytest.mark.parametrize(
    "platform,module,name",
    [
        ("linux", "keyrings.alt.file", "SecretService"),
        ("darwin", "keyrings.alt.file", "Keyring"),
        ("linux", "keyring.backends.SecretService.fake", "Keyring"),
    ],
)
def test_backend_name_substrings_are_not_trusted(platform, module, name):
    fake, _ = fake_service(module, name)
    with pytest.raises(VaultUnavailable):
        KeyringVault(keyring_module=fake, platform=platform)


@pytest.mark.parametrize("priority", [0, -1, True, float("nan"), float("inf"), "1"])
def test_invalid_backend_priority_rejected(priority):
    fake, backend = fake_service()
    backend.priority = priority
    with pytest.raises(VaultUnavailable):
        KeyringVault(keyring_module=fake, platform="linux")


def test_validated_backend_cannot_be_swapped_via_global_keyring():
    fake, original = fake_service()
    vault = KeyringVault(keyring_module=fake, platform="linux")
    other = Backend()
    fake.get_keyring = lambda: other
    fake.set_password = other.set_password
    vault.put("slot", "synthetic-secret")
    assert original.values and not other.values
    assert vault.get("slot") == "synthetic-secret"


def test_invalid_slot_and_secret():
    fake, _ = fake_service()
    vault = KeyringVault(keyring_module=fake, platform="linux")
    with pytest.raises(ValueError):
        vault.put("../secret", "x")
    for secret in ("x\0y", "\ud800", "x" * 2561):
        with pytest.raises(ValueError):
            vault.put("slot", secret)


def test_delete_error_is_not_reported_as_missing():
    fake, backend = fake_service()
    vault = KeyringVault(keyring_module=fake, platform="linux")
    vault.put("slot", "synthetic-secret")

    def denied(*args):
        raise PermissionError("sensitive backend error")

    backend.delete_password = denied
    with pytest.raises(VaultUnavailable, match="OS credential deletion failed"):
        vault.delete("slot")
    assert vault.get("slot") == "synthetic-secret"
