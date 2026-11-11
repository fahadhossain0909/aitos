"""Bitwarden CLI and 1Password CLI credential-vault adapters.

These adapters run only inside the trusted signer process. They require an
already-unlocked provider CLI session and a pre-created item reference. Secret
values are sent over stdin wherever the CLI supports it; command output and
errors are never copied into exception messages or logs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable


class ProviderVaultError(RuntimeError):
    """A provider CLI is unavailable, locked, timed out, or rejected an action."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _provider_environment(session_env: tuple[str, ...]) -> dict[str, str]:
    """Pass only OS runtime and provider-session variables to the CLI child."""
    allowed = {
        "PATH",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    for key, value in os.environ.items():
        if any(
            key == allowed_key or key.startswith(allowed_key)
            for allowed_key in session_env
        ):
            environment[key] = value
    return environment


@dataclass(frozen=True)
class _CLI:
    executable: str
    timeout_seconds: float = 8.0
    runner: Runner = subprocess.run
    session_env: tuple[str, ...] = ()

    def run(self, args: list[str], *, stdin: str | None = None) -> str:
        if not args or any(type(part) is not str or not part for part in args):
            raise ProviderVaultError("provider_command_invalid")
        try:
            result = self.runner(
                [self.executable, *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                env=_provider_environment(self.session_env),
            )
        except subprocess.TimeoutExpired:
            raise ProviderVaultError("provider_command_timeout") from None
        except (OSError, ValueError):
            raise ProviderVaultError("provider_command_unavailable") from None
        if result.returncode != 0:
            # Provider stderr can contain vault metadata or authentication details.
            raise ProviderVaultError("provider_command_failed")
        return result.stdout


class BitwardenCLIVault:
    """Maps one existing Bitwarden item UUID to two hidden custom fields."""

    FIELD_KEY_ID = "aitos_exchange_key_id"
    FIELD_SECRET = "aitos_exchange_api_secret"

    def __init__(
        self,
        executable: str = "bw",
        *,
        timeout_seconds: float = 8.0,
        runner: Runner = subprocess.run,
    ) -> None:
        self._cli = _CLI(executable, timeout_seconds, runner, ("BW_SESSION",))

    @staticmethod
    def _item(reference: str) -> str:
        if type(reference) is not str or not re.fullmatch(
            (
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
            ),
            reference,
        ):
            raise ProviderVaultError("bitwarden_item_reference_invalid")
        return reference

    @classmethod
    def _field(cls, reference: str) -> str:
        if reference.endswith("/key-id"):
            return cls.FIELD_KEY_ID
        if reference.endswith("/api-secret"):
            return cls.FIELD_SECRET
        raise ProviderVaultError("credential_field_reference_invalid")

    def _get_item(self, item_id: str) -> dict:
        raw = self._cli.run(["get", "item", item_id])
        try:
            item = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raise ProviderVaultError("bitwarden_item_response_invalid") from None
        if not isinstance(item, dict) or item.get("id") != item_id:
            raise ProviderVaultError("bitwarden_item_identity_mismatch")
        fields = item.get("fields")
        if fields is None:
            item["fields"] = []
        elif not isinstance(fields, list):
            raise ProviderVaultError("bitwarden_item_fields_invalid")
        return item

    def get_secret(self, reference: str) -> str | None:
        item_id = self._item(reference.rsplit("/", 1)[0])
        label = self._field(reference)
        item = self._get_item(item_id)
        matches = [
            f for f in item["fields"] if isinstance(f, dict) and f.get("name") == label
        ]
        if len(matches) > 1:
            raise ProviderVaultError("bitwarden_duplicate_credential_field")
        value = matches[0].get("value") if matches else None
        if value is not None and type(value) is not str:
            raise ProviderVaultError("bitwarden_credential_value_invalid")
        return value or None

    def put_secret(self, reference: str, secret: str) -> None:
        item_id = self._item(reference.rsplit("/", 1)[0])
        label = self._field(reference)
        item = self._get_item(item_id)
        matches = [
            f for f in item["fields"] if isinstance(f, dict) and f.get("name") == label
        ]
        if len(matches) > 1:
            raise ProviderVaultError("bitwarden_duplicate_credential_field")
        if matches:
            matches[0].update({"type": 1, "value": secret})
        else:
            item["fields"].append(
                {"name": label, "value": secret, "type": 1, "linkedId": None}
            )
        encoded = self._cli.run(
            ["encode"], stdin=json.dumps(item, separators=(",", ":"))
        )
        if not encoded.strip():
            raise ProviderVaultError("bitwarden_encode_failed")
        # bw edit accepts the encoded JSON on stdin; no secret-bearing argv.
        self._cli.run(["edit", "item", item_id], stdin=encoded.strip())

    def delete_secret(self, reference: str) -> bool:
        item_id = self._item(reference.rsplit("/", 1)[0])
        label = self._field(reference)
        item = self._get_item(item_id)
        old = item["fields"]
        matches = [f for f in old if isinstance(f, dict) and f.get("name") == label]
        if len(matches) > 1:
            raise ProviderVaultError("bitwarden_duplicate_credential_field")
        item["fields"] = [
            f for f in old if not (isinstance(f, dict) and f.get("name") == label)
        ]
        if len(item["fields"]) == len(old):
            return False
        encoded = self._cli.run(
            ["encode"], stdin=json.dumps(item, separators=(",", ":"))
        )
        if not encoded.strip():
            raise ProviderVaultError("bitwarden_encode_failed")
        self._cli.run(["edit", "item", item_id], stdin=encoded.strip())
        return True


class OnePasswordCLIVault:
    """Maps vault/item IDs to concealed custom fields in an existing 1Password item."""

    FIELD_KEY_ID = "aitos_exchange_key_id"
    FIELD_SECRET = "aitos_exchange_api_secret"

    def __init__(
        self,
        executable: str = "op",
        *,
        timeout_seconds: float = 8.0,
        runner: Runner = subprocess.run,
    ) -> None:
        self._cli = _CLI(
            executable,
            timeout_seconds,
            runner,
            ("OP_SERVICE_ACCOUNT_TOKEN", "OP_SESSION_"),
        )

    @staticmethod
    def _reference(reference: str) -> tuple[str, str, str]:
        if type(reference) is not str:
            raise ProviderVaultError("onepassword_reference_invalid")
        parts = reference.split("/")
        if len(parts) != 3 or parts[2] not in {"key-id", "api-secret"}:
            raise ProviderVaultError("onepassword_reference_invalid")
        if not all(re.fullmatch(r"[A-Za-z0-9_-]{4,64}", p or "") for p in parts[:2]):
            raise ProviderVaultError("onepassword_reference_invalid")
        return parts[0], parts[1], parts[2]

    @classmethod
    def _label(cls, part: str) -> str:
        return cls.FIELD_KEY_ID if part == "key-id" else cls.FIELD_SECRET

    def _get_item(self, vault_id: str, item_id: str) -> dict:
        raw = self._cli.run(
            [
                "item",
                "get",
                item_id,
                "--vault",
                vault_id,
                "--format",
                "json",
                "--reveal",
            ]
        )
        try:
            item = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raise ProviderVaultError("onepassword_item_response_invalid") from None
        if not isinstance(item, dict) or item.get("id") != item_id:
            raise ProviderVaultError("onepassword_item_identity_mismatch")
        fields = item.get("fields")
        if fields is None:
            item["fields"] = []
        elif not isinstance(fields, list):
            raise ProviderVaultError("onepassword_item_fields_invalid")
        return item

    def get_secret(self, reference: str) -> str | None:
        vault_id, item_id, part = self._reference(reference)
        item = self._get_item(vault_id, item_id)
        label = self._label(part)
        matches = [
            f for f in item["fields"] if isinstance(f, dict) and f.get("label") == label
        ]
        if len(matches) > 1:
            raise ProviderVaultError("onepassword_duplicate_credential_field")
        value = matches[0].get("value") if matches else None
        if value is not None and type(value) is not str:
            raise ProviderVaultError("onepassword_credential_value_invalid")
        return value or None

    def put_secret(self, reference: str, secret: str) -> None:
        vault_id, item_id, part = self._reference(reference)
        item = self._get_item(vault_id, item_id)
        label = self._label(part)
        matches = [
            f for f in item["fields"] if isinstance(f, dict) and f.get("label") == label
        ]
        if len(matches) > 1:
            raise ProviderVaultError("onepassword_duplicate_credential_field")
        if matches:
            matches[0].update({"type": "CONCEALED", "value": secret})
        else:
            item["fields"].append(
                {"label": label, "type": "CONCEALED", "value": secret}
            )
        # 1Password CLI accepts an item JSON template on stdin.
        # Keep secrets out of argv.
        self._cli.run(
            ["item", "edit", item_id, "--vault", vault_id], stdin=json.dumps(item)
        )

    def delete_secret(self, reference: str) -> bool:
        vault_id, item_id, part = self._reference(reference)
        item = self._get_item(vault_id, item_id)
        label = self._label(part)
        old = item["fields"]
        matches = [f for f in old if isinstance(f, dict) and f.get("label") == label]
        if len(matches) > 1:
            raise ProviderVaultError("onepassword_duplicate_credential_field")
        item["fields"] = [
            f for f in old if not (isinstance(f, dict) and f.get("label") == label)
        ]
        if len(item["fields"]) == len(old):
            return False
        self._cli.run(
            ["item", "edit", item_id, "--vault", vault_id], stdin=json.dumps(item)
        )
        return True


def bitwarden_vault_factory() -> BitwardenCLIVault:
    return BitwardenCLIVault()


def onepassword_vault_factory() -> OnePasswordCLIVault:
    return OnePasswordCLIVault()
