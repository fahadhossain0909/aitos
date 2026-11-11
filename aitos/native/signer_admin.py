"""Narrow local signer credential administration API for the trusted desktop window.

Requests arrive over the Tauri sidecar's stdin, never argv or environment.
Only metadata/status responses are returned; credential values are never
returned to the webview or written to logs.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from aitos.native.credential_profiles import (
    CredentialProfileError,
    CredentialProfileStore,
    CredentialProviderRouter,
    LazyNativeCredentialStore,
    discover_external_vault_backends,
)
from aitos.native.cli_credential_vaults import ProviderVaultError
from aitos.native.credential_vault import VaultUnavailable, get_credential_vault
from aitos.security.signer_credentials import SignerCredentialSlots


MAX_REQUEST_BYTES = 8192
ACTIONS = frozenset({"status", "set-mode", "store-credentials"})


def _data_dir() -> Path:
    default = Path.home() / ".local" / "share" / "aitos-signer"
    path = Path(os.environ.get("AITOS_SIGNER_DATA_DIR", default)).expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise RuntimeError("signer_data_directory_invalid")
    if os.name == "posix":
        path.chmod(0o700)
        if path.stat().st_mode & 0o077:
            raise RuntimeError("signer_data_directory_permissions_invalid")
    return path


def _account(value: object) -> str:
    if type(value) is not str or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", value
    ):
        raise ValueError("invalid_account_alias")
    return value


def _router(profiles: CredentialProfileStore, account: str):
    profile = profiles.resolve(account)
    backends = discover_external_vault_backends() if profile.mode == "external" else {}
    return CredentialProviderRouter(
        profiles=profiles,
        native_store=LazyNativeCredentialStore(get_credential_vault),
        external_backends=backends,
    )


def handle_request(request: object) -> dict:
    if not isinstance(request, dict) or set(request) - {
        "action",
        "account_alias",
        "mode",
        "provider_id",
        "secret_ref",
        "key_id",
        "api_secret",
    }:
        raise ValueError("invalid_signer_admin_request")
    action = request.get("action")
    if action not in ACTIONS:
        raise ValueError("signer_admin_action_not_allowed")
    expected_fields = {
        "status": {"action", "account_alias"},
        "set-mode": {
            "action",
            "account_alias",
            "mode",
            "provider_id",
            "secret_ref",
        }
        if request.get("mode") == "external"
        else {"action", "account_alias", "mode"},
        "store-credentials": {
            "action",
            "account_alias",
            "key_id",
            "api_secret",
        },
    }[action]
    if set(request) != expected_fields:
        raise ValueError("invalid_signer_admin_request")
    account = _account(request.get("account_alias"))
    profiles = CredentialProfileStore(_data_dir() / "credential-profiles.sqlite3")
    try:
        if action == "set-mode":
            mode = request.get("mode")
            if mode == "native":
                if request.get("provider_id") is not None or request.get("secret_ref") is not None:
                    raise ValueError("native_mode_does_not_accept_external_reference")
                profiles.set_account(account, "native")
            elif mode == "external":
                provider_id = request.get("provider_id")
                secret_ref = request.get("secret_ref")
                backends = discover_external_vault_backends()
                if type(provider_id) is not str or provider_id not in backends:
                    raise CredentialProfileError("requested_external_provider_not_installed")
                profiles.set_account(
                    account,
                    "external",
                    provider_id=provider_id,
                    secret_ref=secret_ref,
                )
            else:
                raise ValueError("invalid_credential_mode")
            return {"ok": True, "action": action, "account_alias": account}

        try:
            profile = profiles.resolve(account)
        except CredentialProfileError:
            if action == "status":
                return {
                    "ok": True,
                    "action": action,
                    "account_alias": account,
                    "configured": False,
                    "credential_pair_ready": False,
                }
            raise

        if action == "store-credentials":
            slots = SignerCredentialSlots(_router(profiles, account))
            key_id = request.get("key_id")
            secret = request.get("api_secret")
            slots.save(account, key_id=key_id, api_secret=secret)
            stored_key, stored_secret = slots.resolve(account)
            if stored_key != key_id or stored_secret != secret:
                raise RuntimeError("credential_pair_readback_mismatch")
            return {"ok": True, "action": action, "account_alias": account}

        slots = SignerCredentialSlots(_router(profiles, account))
        try:
            slots.resolve(account)
            ready = True
        except Exception:
            ready = False
        return {
            "ok": True,
            "action": action,
            "account_alias": account,
            "configured": True,
            "mode": profile.mode,
            "provider_id": profile.provider_id,
            "secret_ref": profile.secret_ref,
            "credential_pair_ready": ready,
        }
    finally:
        profiles.close()


def main() -> int:
    raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
    if not raw or len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
        response = {"ok": False, "error": "invalid_signer_admin_request"}
    else:
        try:
            request = json.loads(raw.decode("utf-8"))
            response = handle_request(request)
        except (UnicodeError, json.JSONDecodeError, ValueError):
            response = {"ok": False, "error": "invalid_signer_admin_request"}
        except (CredentialProfileError, ProviderVaultError, VaultUnavailable):
            response = {"ok": False, "error": "credential_provider_unavailable"}
        except Exception:
            response = {"ok": False, "error": "credential_operation_failed"}
    sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
