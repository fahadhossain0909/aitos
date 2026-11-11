import pytest

from aitos.security.signer_credentials import (
    SignerCredentialError,
    SignerCredentialSlots,
)


class MemoryVault:
    def __init__(self):
        self.values = {}

    def put(self, slot, secret):
        self.values[slot] = secret

    def get(self, slot):
        return self.values.get(slot)

    def delete(self, slot):
        return self.values.pop(slot, None) is not None


class FailingVault(MemoryVault):
    def put(self, slot, secret):
        if slot.endswith("api-secret"):
            raise RuntimeError("simulated vault failure")
        super().put(slot, secret)


def test_signer_credentials_round_trip_only_through_injected_vault():
    vault = MemoryVault()
    slots = SignerCredentialSlots(vault)
    ref = slots.save("primary", key_id="exchange-key-01", api_secret="test-secret")
    assert ref.account_alias == "primary"
    assert slots.resolve("primary") == ("exchange-key-01", "test-secret")
    assert slots.delete("primary") is True
    with pytest.raises(SignerCredentialError, match="pair_incomplete"):
        slots.resolve("primary")


def test_partial_pair_is_removed_when_secret_write_fails():
    vault = FailingVault()
    with pytest.raises(SignerCredentialError, match="save_failed"):
        SignerCredentialSlots(vault).save(
            "primary", key_id="exchange-key-01", api_secret="test-secret"
        )
    assert vault.values == {}


@pytest.mark.parametrize("alias", ["../primary", "bad alias", "", "a" * 65])
def test_signer_credential_slots_reject_unsafe_alias(alias):
    with pytest.raises(ValueError, match="invalid_account_alias"):
        SignerCredentialSlots(MemoryVault()).save(
            alias, key_id="exchange-key-01", api_secret="test-secret"
        )


def test_signer_credentials_fail_closed_on_missing_pair():
    with pytest.raises(SignerCredentialError, match="pair_incomplete"):
        SignerCredentialSlots(MemoryVault()).resolve("primary")
