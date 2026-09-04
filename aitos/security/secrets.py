"""Secrets boundary with environment and Vault backends.

Production code should request secrets through this boundary rather than
reading secret files or environment variables throughout the application.
The Vault backend uses the Kubernetes/Token auth HTTP contract without making
Vault a hard dependency; the provider can therefore be deployed incrementally.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class SecretProvider:
    def get(self, name: str, *, required: bool = True) -> str | None:
        raise NotImplementedError


@dataclass(frozen=True)
class EnvironmentSecretProvider(SecretProvider):
    prefix: str = ""

    def get(self, name: str, *, required: bool = True) -> str | None:
        value = os.getenv(f"{self.prefix}{name}")
        if required and not value:
            raise RuntimeError(f"required secret {name!r} is not configured")
        return value


@dataclass(frozen=True)
class VaultSecretProvider(SecretProvider):
    address: str
    token: str
    mount: str = "secret"
    path: str = "aitos"
    timeout_seconds: float = 5.0

    def get(self, name: str, *, required: bool = True) -> str | None:
        url = f"{self.address.rstrip('/')}/v1/{self.mount}/data/{self.path}"
        request = Request(
            url,
            headers={"X-Vault-Token": self.token, "Accept": "application/json"},
        )
        try:
            with urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                document = json.loads(response.read().decode("utf-8"))
            value = document.get("data", {}).get("data", {}).get(name)
        except (
            HTTPError,
            URLError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise RuntimeError(f"Vault secret lookup failed for {name!r}") from exc
        if required and not value:
            raise RuntimeError(f"required secret {name!r} is missing from Vault")
        return value


def build_secret_provider() -> SecretProvider:
    backend = os.getenv("SECRETS_BACKEND", "env").strip().lower()
    if backend == "env":
        return EnvironmentSecretProvider()
    if backend == "vault":
        address = os.getenv("VAULT_ADDR")
        token_file = os.getenv("VAULT_TOKEN_FILE", "/run/secrets/vault_token")
        if not address:
            raise RuntimeError("VAULT_ADDR is required when SECRETS_BACKEND=vault")
        try:
            token = open(token_file, encoding="utf-8").read().strip()
        except OSError as exc:
            raise RuntimeError("Vault token file is unavailable") from exc
        if not token:
            raise RuntimeError("Vault token is empty")
        return VaultSecretProvider(
            address=address,
            token=token,
            mount=os.getenv("VAULT_MOUNT", "secret"),
            path=os.getenv("VAULT_PATH", "aitos"),
        )
    raise RuntimeError(f"unsupported SECRETS_BACKEND={backend!r}")
