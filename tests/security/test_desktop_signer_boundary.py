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
    assert "url.port().is_none()" in rust
    assert "url.port() == Some(1420)" in rust
    assert "trusted_local_signer_origin_required" in rust
    assert "WebviewUrl::External(url)" in rust
    assert "invoke_handler" in rust
    assert "open_signer_settings" in rust
    assert "CmdOrCtrl+Shift+S" in rust
    assert "Submenu::with_items" in rust
    assert "api.prevent_close()" in rust
    assert "window.hide()" in rust
    assert "window.set_focus()" in rust


def test_operator_console_has_no_credential_ipc_and_local_signer_is_bundled():
    app = (ROOT / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "invoke(" not in app
    assert "aitos-credential-policy-draft-v1" not in app
    assert "Trusted Signer Settings" in app
    assert "Open Trusted Signer Settings" in app
    assert "Native desktop application" in app
    assert "Native packages are not built yet" not in app

    html = (ROOT / "desktop" / "signer.html").read_text(encoding="utf-8")
    assert 'src="/src/signer.tsx"' in html
    assert "default-src 'self'" in html
    vite = (ROOT / "desktop" / "vite.config.ts").read_text(encoding="utf-8")
    assert 'signer: resolve(desktopRoot, "signer.html")' in vite

    ui = (ROOT / "desktop" / "src" / "SignerSettings.tsx").read_text(encoding="utf-8")
    assert 'invoke<string>("signer_credential_admin"' in ui
    assert "if (result)" in ui
    assert 'setApiSecret("")' in ui
    assert "trustedHostConfirmed" in ui
    assert "if (!e.target.checked)" in ui
    assert "Global default" in ui
    assert "Per-account override" in ui
    assert "shared by every account" in ui
    assert "provider-managed RDP/VPS" in ui
    assert "localStorage" not in ui
    assert "sessionStorage" not in ui
    assert 'type="password"' in ui
    assert "disabled={!available || busy}" in ui
    assert (
        'setAccount(e.target.value); setKeyId(""); setApiSecret(""); setConfirmSecret("");'
        in ui
    )


def test_signer_command_payload_is_stdin_only_and_not_forwarded_to_operator():
    embedded = (ROOT / "aitos" / "native" / "embedded_gateway.py").read_text(
        encoding="utf-8"
    )
    assert 'sys.argv[1:] == ["--credential-admin"]' in embedded
    assert "signer_admin_main()" in embedded

    rust = (TAURI / "src" / "main.rs").read_text(encoding="utf-8")
    assert '.args(["--credential-admin"])' in rust
    assert "process.write(&input)" in rust
    assert "request.len() > 8192" in rust
    assert "struct AdminChild" in rust
    assert "impl Drop for AdminChild" in rust
    assert "input.zeroize()" in rust
    assert "signer_credential_admin" in rust
