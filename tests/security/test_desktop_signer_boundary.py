"""Security tests for the remote operator and trusted local signer surfaces."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TAURI = ROOT / "desktop" / "src-tauri"


def test_only_bundled_signer_window_receives_native_ipc_capability():
    config = json.loads((TAURI / "tauri.conf.json").read_text(encoding="utf-8"))
    assert config["app"]["security"]["capabilities"] == ["signer-local"]
    capability = json.loads(
        (TAURI / "capabilities" / "signer-local.json").read_text(encoding="utf-8")
    )
    assert capability["windows"] == ["signer"]

    rust = (TAURI / "src" / "main.rs").read_text(encoding="utf-8")
    assert 'window.label() != "signer"' in rust
    assert 'url.path() == "/signer.html"' in rust
    assert "trusted_local_signer_origin_required" in rust
    assert 'WebviewUrl::External(url)' in rust
    assert "invoke_handler" in rust


def test_operator_console_has_no_credential_ipc_and_local_signer_is_bundled():
    app = (ROOT / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "invoke(" not in app
    assert "aitos-credential-policy-draft-v1" not in app
    assert "Trusted Signer Settings" in app

    html = (ROOT / "desktop" / "signer.html").read_text(encoding="utf-8")
    assert 'src="/src/signer.tsx"' in html
    assert "default-src 'self'" in html

    ui = (ROOT / "desktop" / "src" / "SignerSettings.tsx").read_text(
        encoding="utf-8"
    )
    assert 'invoke<string>("signer_credential_admin"' in ui
    assert "localStorage" not in ui
    assert "sessionStorage" not in ui
    assert "type=\"password\"" in ui


def test_signer_command_payload_is_stdin_only_and_not_forwarded_to_operator():
    rust = (TAURI / "src" / "main.rs").read_text(encoding="utf-8")
    assert '.args(["--credential-admin"])' in rust
    assert "child.write(&input)" in rust
    assert "request.len() > 8192" in rust
    assert "signer_credential_admin" in rust
