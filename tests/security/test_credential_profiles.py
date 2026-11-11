import pytest

from aitos.native.credential_profiles import (
    CredentialProfileError,
    CredentialProfileStore,
    CredentialProviderRouter,
)
from aitos.native.credential_vault import VaultUnavailable


class MemoryNativeVault:
    def __init__(self):
        self.values = {}

    def put(self, slot, secret):
        self.values[slot] = secret

    def get(self, slot):
        return self.values.get(slot)

    def delete(self, slot):
        return self.values.pop(slot, None) is not None


class MemoryExternalVault:
    def __init__(self):
        self.values = {}

    def put_secret(self, reference, secret):
        self.values[reference] = secret

    def get_secret(self, reference):
        return self.values.get(reference)

    def delete_secret(self, reference):
        return self.values.pop(reference, None) is not None


def test_default_native_and_per_account_external_override(tmp_path):
    profiles = CredentialProfileStore(tmp_path / "profiles.sqlite3")
    native = MemoryNativeVault()
    external = MemoryExternalVault()
    profiles.set_default("native")
    profiles.set_account(
        "remote",
        "external",
        provider_id="bitwarden",
        secret_ref="aitos/exchanges/remote",
    )
    router = CredentialProviderRouter(
        profiles=profiles,
        native_store=native,
        external_backends={"bitwarden": external},
    )

    router.put("signer/local/key-id", "local-key")
    router.put("signer/local/api-secret", "local-secret")
    router.put("signer/remote/key-id", "remote-key")
    router.put("signer/remote/api-secret", "remote-secret")

    assert native.get("signer/local/api-secret") == "local-secret"
    assert (
        external.get_secret("aitos/exchanges/remote/api-secret") == "remote-secret"
    )
    assert router.get("signer/remote/key-id") == "remote-key"
    assert "signer/remote/api-secret" not in native.values
    assert router.delete("signer/remote/api-secret")
    assert router.get("signer/remote/api-secret") is None
    profiles.close()


def test_global_external_policy_routes_accounts_without_overrides(tmp_path):
    profiles = CredentialProfileStore(tmp_path / "profiles.sqlite3")
    external = MemoryExternalVault()
    profiles.set_default(
        "external", provider_id="vaultx", secret_ref="trading/default"
    )
    router = CredentialProviderRouter(
        profiles=profiles,
        native_store=MemoryNativeVault(),
        external_backends={"vaultx": external},
    )
    router.put("signer/account-a/api-secret", "secret-a")
    assert external.get_secret("trading/default/api-secret") == "secret-a"
    profiles.close()


def test_no_implicit_native_fallback_when_external_provider_missing(tmp_path):
    profiles = CredentialProfileStore(tmp_path / "profiles.sqlite3")
    native = MemoryNativeVault()
    profiles.set_default(
        "external", provider_id="missing", secret_ref="vault/account"
    )
    router = CredentialProviderRouter(profiles=profiles, native_store=native)
    with pytest.raises(VaultUnavailable, match="provider unavailable"):
        router.put("signer/account/api-secret", "secret")
    assert native.values == {}
    profiles.close()


def test_missing_profile_and_bad_slots_fail_closed(tmp_path):
    profiles = CredentialProfileStore(tmp_path / "profiles.sqlite3")
    router = CredentialProviderRouter(
        profiles=profiles, native_store=MemoryNativeVault()
    )
    with pytest.raises(CredentialProfileError, match="not_configured"):
        router.get("signer/account/api-secret")
    profiles.set_default("native")
    with pytest.raises(ValueError, match="invalid_signer_credential_slot"):
        router.get("signer/../api-secret")
    profiles.close()


@pytest.mark.parametrize(
    "mode,provider,reference",
    [
        ("unknown", None, None),
        ("native", "vaultx", "secret"),
        ("external", None, "secret"),
        ("external", "vaultx", None),
    ],
)
def test_invalid_profile_combinations_rejected(tmp_path, mode, provider, reference):
    profiles = CredentialProfileStore(tmp_path / "profiles.sqlite3")
    with pytest.raises(ValueError):
        profiles.set_default(mode, provider_id=provider, secret_ref=reference)
    profiles.close()


def test_profiles_persist_without_persisting_secret_values(tmp_path):
    path = tmp_path / "profiles.sqlite3"
    profiles = CredentialProfileStore(path)
    profiles.set_account(
        "acct",
        "external",
        provider_id="onepassword",
        secret_ref="vault/item-123",
    )
    profiles.close()

    reopened = CredentialProfileStore(path)
    profile = reopened.resolve("acct")
    assert profile.mode == "external"
    assert profile.provider_id == "onepassword"
    assert profile.secret_ref == "vault/item-123"
    assert b"exchange-secret" not in path.read_bytes()
    reopened.close()
