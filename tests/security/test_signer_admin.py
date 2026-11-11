from aitos.native import signer_admin


class MemoryVault:
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


def test_native_mode_store_and_status_never_return_secret_values(tmp_path, monkeypatch):
    vault = MemoryVault()
    monkeypatch.setattr(signer_admin, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(signer_admin, "get_credential_vault", lambda: vault)

    policy = signer_admin.handle_request(
        {
            "action": "set-mode",
            "account_alias": "testnet-primary",
            "mode": "native",
        }
    )
    assert policy["ok"] is True

    stored = signer_admin.handle_request(
        {
            "action": "store-credentials",
            "account_alias": "testnet-primary",
            "key_id": "synthetic-key-id",
            "api_secret": "synthetic-api-secret",
        }
    )
    assert stored == {
        "ok": True,
        "action": "store-credentials",
        "account_alias": "testnet-primary",
    }

    status = signer_admin.handle_request(
        {"action": "status", "account_alias": "testnet-primary"}
    )
    assert status["credential_pair_ready"] is True
    assert "synthetic-key-id" not in repr(status)
    assert "synthetic-api-secret" not in repr(status)


def test_external_mode_routes_pair_to_only_selected_provider(tmp_path, monkeypatch):
    provider = MemoryExternalVault()
    monkeypatch.setattr(signer_admin, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        signer_admin, "discover_external_vault_backends", lambda: {"bitwarden": provider}
    )

    signer_admin.handle_request(
        {
            "action": "set-mode",
            "account_alias": "testnet-secondary",
            "mode": "external",
            "provider_id": "bitwarden",
            "secret_ref": "00000000-0000-4000-8000-000000000001",
        }
    )
    signer_admin.handle_request(
        {
            "action": "store-credentials",
            "account_alias": "testnet-secondary",
            "key_id": "synthetic-external-key",
            "api_secret": "synthetic-external-secret",
        }
    )

    assert provider.values == {
        "00000000-0000-4000-8000-000000000001/key-id": "synthetic-external-key",
        "00000000-0000-4000-8000-000000000001/api-secret": "synthetic-external-secret",
    }


def test_unconfigured_account_status_is_non_mutating(tmp_path, monkeypatch):
    monkeypatch.setattr(signer_admin, "_data_dir", lambda: tmp_path)
    result = signer_admin.handle_request(
        {"action": "status", "account_alias": "not-configured"}
    )
    assert result["configured"] is False
    assert result["credential_pair_ready"] is False


def test_status_rejects_secret_fields_even_if_they_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(signer_admin, "_data_dir", lambda: tmp_path)
    try:
        signer_admin.handle_request(
            {
                "action": "status",
                "account_alias": "safe",
                "api_secret": "must-not-be-accepted",
            }
        )
    except ValueError as exc:
        assert str(exc) == "invalid_signer_admin_request"
    else:
        raise AssertionError("status must not accept credential material")



def test_stdin_protocol_returns_only_metadata_and_never_secret_material(
    tmp_path, monkeypatch
):
    import io
    import json

    vault = MemoryVault()
    monkeypatch.setattr(signer_admin, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(signer_admin, "get_credential_vault", lambda: vault)
    signer_admin.handle_request(
        {
            "action": "set-mode",
            "account_alias": "stdin-account",
            "mode": "native",
        }
    )
    signer_admin.handle_request(
        {
            "action": "store-credentials",
            "account_alias": "stdin-account",
            "key_id": "synthetic-stdin-key",
            "api_secret": "synthetic-stdin-secret",
        }
    )
    monkeypatch.setattr(
        signer_admin.sys,
        "stdin",
        type("Input", (), {"buffer": io.BytesIO(
            json.dumps(
                {"action": "status", "account_alias": "stdin-account"}
            ).encode("utf-8") + b"\\n"
        )})(),
    )
    output = io.StringIO()
    monkeypatch.setattr(signer_admin.sys, "stdout", output)

    assert signer_admin.main() == 0
    rendered = output.getvalue()
    assert json.loads(rendered)["credential_pair_ready"] is True
    assert "synthetic-stdin-key" not in rendered
    assert "synthetic-stdin-secret" not in rendered

def test_unknown_fields_and_actions_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(signer_admin, "_data_dir", lambda: tmp_path)
    try:
        signer_admin.handle_request(
            {
                "action": "status",
                "account_alias": "safe",
                "unexpected": "must-not-be-accepted",
            }
        )
    except ValueError as exc:
        assert str(exc) == "invalid_signer_admin_request"
    else:
        raise AssertionError("unknown request fields must be rejected")

    try:
        signer_admin.handle_request(
            {"action": "delete-all", "account_alias": "safe"}
        )
    except ValueError as exc:
        assert str(exc) == "signer_admin_action_not_allowed"
    else:
        raise AssertionError("unknown operations must be rejected")
