import base64
import json
import subprocess

import pytest

from aitos.native.cli_credential_vaults import (
    BitwardenCLIVault,
    OnePasswordCLIVault,
    ProviderVaultError,
)


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return subprocess.CompletedProcess(
            args=args,
            returncode=response[0],
            stdout=response[1],
            stderr="sensitive provider diagnostic must not escape",
        )


def test_bitwarden_put_sends_encoded_item_over_stdin_not_argv():
    item_id = "12345678-1234-1234-1234-123456789abc"
    original = {"id": item_id, "fields": []}
    encoded = base64.b64encode(json.dumps(original).encode()).decode()
    runner = FakeRunner([(0, json.dumps(original)), (0, encoded), (0, "{}")])
    vault = BitwardenCLIVault(runner=runner)

    vault.put_secret(f"{item_id}/api-secret", "exchange-secret-value")

    assert runner.calls[0][0] == ["bw", "get", "item", item_id]
    assert runner.calls[1][1]["input"]
    assert "exchange-secret-value" in runner.calls[1][1]["input"]
    assert runner.calls[2][0] == ["bw", "edit", "item", item_id]
    assert runner.calls[2][1]["input"] == encoded
    assert all("exchange-secret-value" not in " ".join(call[0]) for call in runner.calls)


def test_bitwarden_get_reads_only_named_field():
    item_id = "12345678-1234-1234-1234-123456789abc"
    item = {
        "id": item_id,
        "fields": [
            {"name": BitwardenCLIVault.FIELD_SECRET, "value": "secret", "type": 1}
        ],
    }
    runner = FakeRunner([(0, json.dumps(item))])
    vault = BitwardenCLIVault(runner=runner)
    assert vault.get_secret(f"{item_id}/api-secret") == "secret"


def test_onepassword_put_uses_stdin_and_concealed_field():
    item = {"id": "item1234", "fields": []}
    runner = FakeRunner([(0, json.dumps(item)), (0, "{}")])
    vault = OnePasswordCLIVault(runner=runner)

    vault.put_secret("vault1234/item1234/api-secret", "secret-value")

    assert runner.calls[0][0] == [
        "op", "item", "get", "item1234", "--vault", "vault1234",
        "--format", "json", "--reveal",
    ]
    assert runner.calls[1][0] == [
        "op", "item", "edit", "item1234", "--vault", "vault1234"
    ]
    edited = json.loads(runner.calls[1][1]["input"])
    assert edited["fields"][0]["type"] == "CONCEALED"
    assert edited["fields"][0]["value"] == "secret-value"
    assert "secret-value" not in " ".join(runner.calls[1][0])


def test_onepassword_get_rejects_duplicate_fields():
    item = {
        "id": "item1234",
        "fields": [
            {"label": OnePasswordCLIVault.FIELD_SECRET, "value": "one"},
            {"label": OnePasswordCLIVault.FIELD_SECRET, "value": "two"},
        ],
    }
    vault = OnePasswordCLIVault(runner=FakeRunner([(0, json.dumps(item))]))
    with pytest.raises(ProviderVaultError, match="duplicate_credential_field"):
        vault.get_secret("vault1234/item1234/api-secret")


def test_provider_timeout_is_redacted():
    runner = FakeRunner([subprocess.TimeoutExpired(["bw"], 1)])
    vault = BitwardenCLIVault(runner=runner, timeout_seconds=1)
    with pytest.raises(ProviderVaultError, match="provider_command_timeout"):
        vault.get_secret("12345678-1234-1234-1234-123456789abc/api-secret")


@pytest.mark.parametrize(
    "vault,reference",
    [
        (BitwardenCLIVault(runner=FakeRunner([])), "not-a-uuid/api-secret"),
        (OnePasswordCLIVault(runner=FakeRunner([])), "bad-reference/api-secret"),
    ],
)
def test_provider_rejects_invalid_item_reference_before_cli(vault, reference):
    with pytest.raises(ProviderVaultError):
        vault.get_secret(reference)
