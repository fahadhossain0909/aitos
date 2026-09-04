from __future__ import annotations

import json

import pytest

from aitos.security.secrets import (
    EnvironmentSecretProvider,
    VaultSecretProvider,
    build_secret_provider,
)


def test_environment_provider_fails_closed_for_required_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AITOS_TEST_SECRET", raising=False)
    provider = EnvironmentSecretProvider(prefix="AITOS_")
    with pytest.raises(RuntimeError, match="required secret"):
        provider.get("TEST_SECRET")


def test_environment_provider_reads_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AITOS_TEST_SECRET", "value")
    assert EnvironmentSecretProvider(prefix="AITOS_").get("TEST_SECRET") == "value"


def test_vault_provider_reads_kv_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"data": {"data": {"API_KEY": "secret"}}}).encode()

    monkeypatch.setattr(
        "aitos.security.secrets.urlopen", lambda *args, **kwargs: Response()
    )
    provider = VaultSecretProvider("http://vault", "token")
    assert provider.get("API_KEY") == "secret"


def test_vault_backend_requires_token_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("SECRETS_BACKEND", "vault")
    monkeypatch.setenv("VAULT_ADDR", "http://vault")
    monkeypatch.setenv("VAULT_TOKEN_FILE", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="token file"):
        build_secret_provider()
